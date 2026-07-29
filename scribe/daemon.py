"""The local daemon: watcher, HTTP + SSE server, and the hooks' control socket.

One daemon serves every project on the machine. That is the design decision
that fixes the previous generation's worst limitation — parallel sessions
interleaving into a single append-only file — and it means there is one URL to
bookmark rather than one server per repo.

Three things run here:

* a **watcher** thread that polls transcript mtimes and rebuilds what changed;
* an **HTTP server** serving the viewer, a JSON API, and an SSE stream;
* a **control socket** that hook processes talk to (see :mod:`scribe.hookclient`).

Nothing here ever writes to a transcript, and nothing enters a session's
context window.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import socket
import socketserver
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (
    archive,
    build,
    config,
    control,
    explain,
    paths,
    redact,
    render_json,
    search,
    store,
    transcript,
)

# Inside the package, not beside it: a wheel that shipped a top-level
# `viewer/` into site-packages would collide with any other project doing
# the same, and `import viewer` would resolve to ours.
VIEWER_DIR = Path(__file__).resolve().parent / "viewer"
PORT_ATTEMPTS = 20
INDEX_RESCAN_S = 4.0
IDLE_EVICT_S = 900
MAX_LOADED = 8


# ==================================================================== hub


class LiveSession:
    """One transcript, its parsed rows, its model, and its rendered payload."""

    def __init__(self, ref: transcript.SessionRef, cfg: dict, explainer=None):
        self.ref = ref
        self.cfg = cfg
        self.explainer = explainer
        self.tail = transcript.TranscriptTail(path=ref.path)
        self.rows: list[dict] = []
        self.session = None
        self.rounds_json: list[dict] = []
        self.head_json: dict = {}
        self.touched = time.time()
        self.dirty = True
        self.web_messages: list[dict] = []
        self._lock = threading.RLock()

    @property
    def id(self) -> str:
        return self.ref.session_id

    def poll(self) -> bool:
        """Read anything new. Returns True if the model changed."""
        new_rows = self.tail.read_new()
        restarted = self.tail.restarted
        if not new_rows and not restarted and self.session is not None:
            return False
        with self._lock:
            if restarted:
                # The transcript was rewritten under us; everything we hold is
                # a stale copy of what we just re-read.
                self.rows = []
            elif self.tail.offset and not self.rows and not new_rows:
                return False
            self.rows.extend(new_rows)
            self.dirty = True
        return True

    def rebuild(self, redactor) -> tuple[list[dict], list[int]]:
        """Rebuild the model. Returns (changed_round_payloads, removed_indices)."""
        with self._lock:
            rows = list(self.rows)
            self.dirty = False
        # Subagents live in their own files rather than in the tailed stream, so
        # they are reloaded on each rebuild. Cheap: the directory is usually
        # absent, and when present it holds a handful of files.
        session = build.build(
            rows,
            transcript_path=str(self.ref.path),
            cwd_hint=self.ref.cwd,
            subagents=transcript.load_subagents(self.ref.path),
        )
        store.annotate(session)
        self._merge_web_messages(session)
        explain.attach(session, self.explainer, self.cfg)
        self.session = session
        self.touched = time.time()

        renderer = render_json.JsonRenderer(redactor)
        fresh = [_key_round(renderer.round(r)) for r in session.rounds]
        old = self.rounds_json
        changed = [r for i, r in enumerate(fresh) if i >= len(old) or old[i] != r]
        removed = list(range(len(fresh), len(old)))
        self.rounds_json = fresh
        self.head_json = renderer.session(session, include_rounds=False)
        self.head_json["armed"] = False  # filled in by the hub
        return changed, removed

    def _merge_web_messages(self, session) -> None:
        """Show replies sent from the browser as rounds of their own.

        A message injected through the Stop hook reaches Claude as the *reason*
        for a blocked stop, not as a user message, so it never appears in the
        transcript as one. Without this the log would show Claude answering a
        question nobody asked.
        """
        if not self.web_messages:
            return
        for entry in self.web_messages:
            target = None
            for rnd in session.rounds:
                if rnd.ts and rnd.ts >= entry["ts"]:
                    target = rnd
                    break
            index = session.rounds.index(target) if target else len(session.rounds)
            from .model import Round

            session.rounds.insert(
                index,
                Round(index=0, uuid=entry["id"], ts=entry["ts"], prompt=entry["text"], source="web"),
            )
        for i, rnd in enumerate(session.rounds, start=1):
            rnd.index = i

    def note_web_message(self, text: str) -> None:
        with self._lock:
            self.web_messages.append(
                {
                    "id": f"web-{len(self.web_messages)}",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                    "text": text,
                }
            )
            self.dirty = True


find_call = explain.find_call


def _key_round(payload: dict) -> dict:
    """Give every item a stable key and a content hash.

    The key lets the viewer reconcile in place: transcripts are append-only, so
    an item's position within its round never changes. Tool calls key on their
    own id; everything else keys on position.

    The hash is what makes that cheap. The live round is re-sent whenever it
    grows, and it can hold a hundred tool calls carrying tens of kilobytes of
    output each — having the browser stringify all of that several times a
    second to spot the one item that changed is the kind of cost that shows up
    as jank. Python does it once instead.
    """
    for i, item in enumerate(payload.get("items", [])):
        if item.get("kind") == "tool" and item.get("id"):
            item["key"] = "t:" + item["id"]
        else:
            item["key"] = f"{payload['index']}:{i}"
        item["h"] = hashlib.blake2b(
            json.dumps(item, sort_keys=True, default=str).encode("utf-8"), digest_size=8
        ).hexdigest()
    return payload


class Subscriber:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.queue: queue.Queue = queue.Queue(maxsize=256)
        self.alive = True

    def send(self, event: str, data) -> None:
        if not self.alive:
            return
        try:
            self.queue.put_nowait((event, data))
        except queue.Full:
            # A wedged client must not hold the daemon's memory hostage.
            self.alive = False


class Hub:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.redactor = redact.from_config(cfg)
        self.control = control.ControlState()
        self.explainer = explain.Explainer(cfg, on_ready=self._on_explanation)
        self.sessions: dict[str, LiveSession] = {}
        self.index: list[transcript.SessionRef] = []
        self.subscribers: list[Subscriber] = []
        self.registered: dict[str, float] = {}  # session_id -> last hook contact
        self._explain_origin: dict[str, str] = {}  # call_id -> session, for on-demand
        self._lock = threading.RLock()
        self._last_index_scan = 0.0
        self.archive_stats = archive.Stats()
        self.search = search.get_index()
        self._search_busy = False
        self.started = time.time()
        self.refresh_index(force=True)

    # -- index ----------------------------------------------------------

    def refresh_index(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self._last_index_scan < INDEX_RESCAN_S:
            return False
        self._last_index_scan = now
        # Sweep the transcripts our own explainer children leave behind. They
        # are already invisible in the viewer; this stops them accumulating a
        # few kilobytes at a time in ~/.claude/projects forever.
        try:
            explain.prune_transcripts()
        except Exception:
            pass
        fresh = transcript.index_sessions()
        # Archive on the same pass. Claude Code deletes transcripts after 30
        # days, so this is the only thing standing between the index we just
        # built and losing all of it.
        try:
            self.archive_stats = archive.sweep(fresh)
        except Exception:
            pass
        self.request_search_sync(fresh)
        with self._lock:
            changed = [r.session_id for r in fresh] != [r.session_id for r in self.index]
            self.index = fresh
        return changed

    def index_payload(self) -> list[dict]:
        with self._lock:
            refs = list(self.index)
        out = []
        for ref in refs:
            item = ref.as_dict()
            item["live"] = ref.session_id in self.registered
            item["armed"] = self.control.is_armed(ref.session_id)
            out.append(item)
        return out

    def request_search_sync(self, refs=None) -> None:
        """Reindex changed sessions on a worker thread.

        Never on the request path: a cold index is a one-off ~1s over the whole
        corpus, and the daemon should serve the viewer while it happens.
        """
        with self._lock:
            if self._search_busy:
                return
            self._search_busy = True
        snapshot = list(refs) if refs is not None else None

        def work():
            try:
                self.search.sync(snapshot)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._search_busy = False

        threading.Thread(target=work, daemon=True, name="scribe-search").start()

    def archive_state(self) -> dict:
        stats = getattr(self, "archive_stats", None)
        return {
            "last_sweep": stats.as_dict() if stats else {},
            "summary": archive.summary(),
        }

    def ref_for(self, session_id: str) -> transcript.SessionRef | None:
        with self._lock:
            for ref in self.index:
                if ref.session_id == session_id:
                    return ref
        self.refresh_index(force=True)
        with self._lock:
            for ref in self.index:
                if ref.session_id == session_id:
                    return ref
        return None

    # -- sessions -------------------------------------------------------

    def get(self, session_id: str, create: bool = True) -> LiveSession | None:
        with self._lock:
            live = self.sessions.get(session_id)
            if live is not None:
                live.touched = time.time()
                return live
        if not create:
            return None
        ref = self.ref_for(session_id)
        if ref is None:
            return None
        live = LiveSession(ref, self.cfg, self.explainer)
        with self._lock:
            self.sessions[session_id] = live
        self.poll_session(live, announce=False)
        self._evict()
        return live

    def _evict(self) -> None:
        with self._lock:
            if len(self.sessions) <= MAX_LOADED:
                return
            watched = {s.session_id for s in self.subscribers if s.alive}
            now = time.time()
            candidates = [
                (live.touched, sid)
                for sid, live in self.sessions.items()
                if sid not in watched
                and sid not in self.registered
                and now - live.touched > IDLE_EVICT_S / 4
            ]
            candidates.sort()
            for _, sid in candidates[: max(0, len(self.sessions) - MAX_LOADED)]:
                self.sessions.pop(sid, None)

    def poll_session(self, live: LiveSession, announce: bool = True) -> None:
        try:
            if not live.poll() and not live.dirty:
                return
            # Archive before rendering. If anything below throws, the bytes are
            # already safe — that ordering is the whole point.
            try:
                archive.archive_one(live.ref)
            except Exception:
                pass
            changed, removed = live.rebuild(self.redactor)
            if live.session is not None:
                store.write_markdown(live.session, self.cfg)
        except Exception:
            sys.stderr.write("scribe: rebuild failed\n" + traceback.format_exc())
            return
        if not announce:
            return
        if changed or removed:
            self.broadcast(
                live.id,
                "rounds",
                {"rounds": changed, "removed": removed, "head": self.head_for(live)},
            )

    def head_for(self, live: LiveSession) -> dict:
        head = dict(live.head_json)
        head["armed"] = self.control.is_armed(live.id)
        head["live"] = live.id in self.registered
        head["queued"] = self.control.queued(live.id)
        head["chain"] = self.control.chain_count(live.id)
        head["remote_approval"] = bool((self.cfg.get("remote_approval") or {}).get("enabled"))
        head["reply_queue"] = bool((self.cfg.get("reply_queue") or {}).get("enabled"))
        return head

    def snapshot(self, session_id: str) -> dict | None:
        live = self.get(session_id)
        if live is None:
            return None
        self.poll_session(live, announce=False)
        return {
            "head": self.head_for(live),
            "rounds": live.rounds_json,
            "pending": [c.as_dict() for c in self.control.pending_for(session_id)],
        }

    # -- subscriptions --------------------------------------------------

    def subscribe(self, session_id: str) -> Subscriber:
        sub = Subscriber(session_id)
        with self._lock:
            self.subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        sub.alive = False
        with self._lock:
            if sub in self.subscribers:
                self.subscribers.remove(sub)

    def has_clients(self, session_id: str) -> bool:
        with self._lock:
            return any(s.alive and s.session_id == session_id for s in self.subscribers)

    def broadcast(self, session_id: str | None, event: str, data) -> None:
        with self._lock:
            targets = [
                s
                for s in self.subscribers
                if s.alive and (session_id is None or s.session_id == session_id)
            ]
        for sub in targets:
            sub.send(event, data)

    # -- hook-facing ----------------------------------------------------

    def reload_config(self) -> None:
        """Re-read settings without restarting.

        `scribe config set` pokes the daemon so a flag like
        `remote_approval.enabled` takes effect on the next tool call rather than
        the next restart. The dict is mutated in place because handlers and
        live sessions already hold a reference to it.
        """
        fresh = config.load()
        self.cfg.clear()
        self.cfg.update(fresh)
        self.redactor = redact.from_config(self.cfg)
        self.explainer.cfg = self.cfg
        for live in list(self.sessions.values()):
            live.cfg = self.cfg
            live.dirty = True
        self.broadcast(None, "config", {"ok": True})

    def register(self, session_id: str, cwd: str = "") -> None:
        if not session_id:
            return
        with self._lock:
            first = session_id not in self.registered
            self.registered[session_id] = time.time()
        if first:
            self.refresh_index(force=True)
            self.broadcast(None, "sessions", self.index_payload())

    def poke(self, session_id: str) -> None:
        """A hook says something happened; look now instead of on the next tick."""
        self.register(session_id)
        live = self.get(session_id)
        if live is not None:
            self.poll_session(live)

    def explain_call(self, session_id: str, call) -> None:
        """Ask for an explanation of an already-recorded call."""
        with self._lock:
            self._explain_origin[call.id] = session_id
        self.explainer.request(call.id, call.name, call.input, force=True)

    def _on_explanation(self, call_id: str, text: str, tier: int) -> None:
        call = self.control.annotate(call_id, text, tier)
        if call is not None:
            self.broadcast(call.session_id, "pending", call.as_dict())
            session_id = call.session_id
        else:
            with self._lock:
                session_id = self._explain_origin.pop(call_id, "")
        # The explanation is now cached, so a rebuild folds it into the model,
        # the markdown, and the rail in one step.
        live = self.get(session_id, create=False) if session_id else None
        if live is not None:
            live.dirty = True
            self.poll_session(live)

    # -- the watcher ----------------------------------------------------

    def tick(self) -> None:
        if self.refresh_index():
            self.broadcast(None, "sessions", self.index_payload())

        window = float(self.cfg.get("active_window_min", 180)) * 60
        now = time.time()
        with self._lock:
            watched = {s.session_id for s in self.subscribers if s.alive}
            registered = set(self.registered)
            refs = list(self.index)
            loaded = list(self.sessions.values())

        hot = {r.session_id for r in refs if now - r.mtime < window}
        interesting = hot | watched | registered

        for live in loaded:
            if live.id in interesting:
                self.poll_session(live)

        # A session someone is watching but which is not loaded yet.
        for session_id in watched | registered:
            if session_id not in {l.id for l in loaded}:
                self.get(session_id)

        with self._lock:
            for session_id, last in list(self.registered.items()):
                if now - last > IDLE_EVICT_S:
                    self.registered.pop(session_id, None)


# ==================================================================== HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "scribe"
    hub: Hub = None  # set on the server class

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get("SCRIBE_DEBUG"):
            sys.stderr.write("scribe: " + (fmt % args) + "\n")

    def handle_one_request(self):
        # A browser closing a tab resets the SSE connection mid-read, which
        # BaseHTTPRequestHandler reports as an unhandled traceback. It is the
        # normal way a stream ends, not an error worth printing.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    # -- helpers --------------------------------------------------------

    # Transcripts contain whatever the agent read, which may include hostile
    # markup from a repository. The viewer sanitises what it renders; this is
    # the outer lock, and it also guarantees the page can never reach the
    # network. `script-src 'self'` is why the theme bootstrap lives in its own
    # file instead of an inline <script>.
    CSP = (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
    )

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy", self.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _text(self, text: str, code: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        self._send(code, text.encode("utf-8"), ctype)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 4_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, OSError):
            return {}

    def _guard(self) -> bool:
        """Reject cross-origin and DNS-rebinding attempts.

        The daemon binds loopback only, but a page on any origin can still make
        the browser issue requests to 127.0.0.1. Everything here is
        conversation content, so the Host header is checked and non-simple
        requests must come from our own origin.
        """
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            self._text("bad host", 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            allowed = {f"http://127.0.0.1:{self.server.server_address[1]}",
                       f"http://localhost:{self.server.server_address[1]}"}
            if origin not in allowed:
                self._text("bad origin", 403)
                return False
        return True

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        route = url.path
        params = parse_qs(url.query)

        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route.startswith("/static/"):
            return self._static(route[len("/static/") :])
        if route == "/api/sessions":
            return self._json({"sessions": self.hub.index_payload()})
        if route == "/api/session":
            session_id = (params.get("id") or [""])[0]
            snap = self.hub.snapshot(session_id)
            if snap is None:
                return self._json({"error": "no such session"}, 404)
            return self._json(snap)
        if route == "/api/stream":
            return self._stream((params.get("id") or [""])[0])
        if route == "/api/config":
            return self._json(self.hub.cfg)
        if route == "/api/search":
            query = (params.get("q") or [""])[0]
            try:
                limit = min(1000, max(1, int((params.get("limit") or ["300"])[0])))
            except ValueError:
                limit = 300
            result = self.hub.search.search(query, limit=limit)
            result["indexing"] = self.hub.search.syncing
            return self._json(result)
        if route == "/api/archive":
            state = self.hub.archive_state()
            state["search"] = self.hub.search.stats()
            return self._json(state)
        if route == "/api/health":
            return self._json({"ok": True, "uptime": round(time.time() - self.hub.started, 1)})
        return self._text("not found", 404)

    def do_POST(self):
        if not self._guard():
            return
        route = urlparse(self.path).path
        data = self._body()

        if route == "/api/decision":
            call_id = str(data.get("call_id") or "")
            behavior = str(data.get("behavior") or "")
            if behavior not in ("allow", "deny", "pass"):
                return self._json({"error": "behavior must be allow, deny or pass"}, 400)
            updated = data.get("updated_input")
            ok = self.hub.control.resolve(
                call_id, behavior, updated if isinstance(updated, dict) else None
            )
            call = self.hub.control.get_pending(call_id)
            if call is not None:
                self.hub.broadcast(call.session_id, "pending", call.as_dict())
            return self._json({"ok": ok})

        if route == "/api/message":
            session_id = str(data.get("session_id") or "")
            text = str(data.get("text") or "")
            if not (self.hub.cfg.get("reply_queue") or {}).get("enabled"):
                return self._json({"error": "reply queue is disabled"}, 409)
            depth = self.hub.control.enqueue(session_id, text)
            live = self.hub.get(session_id, create=False)
            if live is not None:
                self.hub.broadcast(session_id, "queue", {"queued": self.hub.control.queued(session_id)})
            return self._json({"ok": bool(depth), "queued": depth})

        if route == "/api/unqueue":
            session_id = str(data.get("session_id") or "")
            index = int(data.get("index") or 0)
            self.hub.control.drop_queued(session_id, index)
            self.hub.broadcast(session_id, "queue", {"queued": self.hub.control.queued(session_id)})
            return self._json({"ok": True})

        if route == "/api/arm":
            session_id = str(data.get("session_id") or "")
            on = bool(data.get("on"))
            if on and not (self.hub.cfg.get("remote_approval") or {}).get("enabled"):
                return self._json({"error": "remote approval is disabled"}, 409)
            self.hub.control.arm(session_id, on)
            live = self.hub.get(session_id, create=False)
            head = self.hub.head_for(live) if live else {"armed": on}
            self.hub.broadcast(session_id, "head", head)
            return self._json({"ok": True, "armed": on})

        if route == "/api/explain":
            # On-demand explanation of any call in any session, including
            # archived ones. Cheap because the answer is cached by content: an
            # opaque command you asked about once is annotated everywhere it
            # ever appears, and in the markdown too.
            session_id = str(data.get("session_id") or "")
            call_id = str(data.get("call_id") or "")
            live = self.hub.get(session_id)
            if live is None or live.session is None:
                return self._json({"error": "no such session"}, 404)
            call = find_call(live.session, call_id)
            if call is None:
                return self._json({"error": "no such call"}, 404)
            cached = self.hub.explainer.lookup(call.name, call.input)
            if cached:
                return self._json({"ok": True, "explanation": cached, "cached": True})
            if not self.hub.explainer.enabled:
                return self._json({"error": "explanations are disabled"}, 409)
            self.hub.explain_call(session_id, call)
            return self._json({"ok": True, "pending": True})

        if route == "/api/rebuild":
            session_id = str(data.get("session_id") or "")
            live = self.hub.get(session_id)
            if live is None:
                return self._json({"error": "no such session"}, 404)
            live.dirty = True
            self.hub.poll_session(live)
            return self._json({"ok": True, "log_path": live.head_json.get("log_path")})

        return self._text("not found", 404)

    # -- static ---------------------------------------------------------

    TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".woff2": "font/woff2",
    }

    def _static(self, name: str) -> None:
        # Resolve inside VIEWER_DIR so a crafted path cannot escape it.
        target = (VIEWER_DIR / name).resolve()
        try:
            target.relative_to(VIEWER_DIR.resolve())
        except ValueError:
            return self._text("not found", 404)
        if not target.is_file():
            return self._text("not found", 404)
        ctype = self.TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    # -- SSE ------------------------------------------------------------

    def _stream(self, session_id: str) -> None:
        sub = self.hub.subscribe(session_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._frame("hello", {"session_id": session_id, "ok": True})
            last_ping = time.time()
            while sub.alive:
                try:
                    event, data = sub.queue.get(timeout=1.0)
                    self._frame(event, data)
                except queue.Empty:
                    if time.time() - last_ping > 15:
                        self._frame("ping", {"t": round(time.time())})
                        last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(sub)

    def _frame(self, event: str, data) -> None:
        payload = json.dumps(data, default=str)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ==================================================================== control socket


class ControlHandler(socketserver.StreamRequestHandler):
    """One newline-delimited JSON request, one newline-delimited JSON reply."""

    hub: Hub = None

    def handle(self):
        try:
            raw = self.rfile.readline(2_000_000)
        except OSError:
            return
        if not raw:
            return
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return self._reply({})
        if not isinstance(payload, dict):
            return self._reply({})
        try:
            self._reply(dispatch(self.hub, payload))
        except Exception:
            sys.stderr.write("scribe: control error\n" + traceback.format_exc())
            self._reply({})

    def _reply(self, data) -> None:
        try:
            self.wfile.write((json.dumps(data) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (OSError, BrokenPipeError):
            pass


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 32

    # Approval holds are long-lived by design; nothing here should time out
    # underneath a hook that is legitimately waiting on a human.
    timeout = None


def dispatch(hub: Hub, payload: dict) -> dict:
    """Handle one hook event. The return value becomes the hook's stdout."""
    event = payload.get("hook_event_name") or payload.get("event") or ""
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""

    if event == "ConfigChanged":
        hub.reload_config()
        return {}

    if event == "SessionStart":
        hub.register(session_id, cwd)
        hub.control.reset_chain(session_id)
        hub.poke(session_id)
        return {}

    if event == "SessionEnd":
        hub.poke(session_id)
        hub.control.forget(session_id)
        return {}

    if event == "UserPromptSubmit":
        # A real prompt from the terminal ends any chain of injected replies.
        hub.control.reset_chain(session_id)
        hub.poke(session_id)
        return {}

    if event in ("PostToolUse", "PostToolUseFailure", "SubagentStop", "Notification"):
        hub.poke(session_id)
        if event == "Notification":
            hub.broadcast(
                session_id,
                "notify",
                {
                    "type": payload.get("notification_type") or "",
                    "message": payload.get("message") or "",
                },
            )
        return {}

    if event == "PermissionRequest":
        return handle_permission(hub, payload)

    if event == "Stop":
        return handle_stop(hub, payload)

    hub.poke(session_id)
    return {}


def handle_permission(hub: Hub, payload: dict) -> dict:
    session_id = payload.get("session_id") or ""
    call_id = str(payload.get("tool_use_id") or "") or f"anon-{time.time_ns()}"
    tool_name = str(payload.get("tool_name") or "Tool")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}

    hub.register(session_id)

    pending = control.PendingCall(
        call_id=call_id,
        session_id=session_id,
        tool_name=tool_name,
        tool_input=hub.redactor.scrub_data(tool_input),
        permission_reason=str(payload.get("permission_reason") or ""),
    )
    canned = hub.explainer.canned(tool_name, tool_input)
    if canned:
        pending.explanation, pending.explanation_tier = canned, 0

    remote = hub.cfg.get("remote_approval") or {}
    armed = hub.control.is_armed(session_id)
    watching = hub.has_clients(session_id)
    hold = bool(remote.get("enabled")) and armed and (watching or not remote.get("require_client", True))
    wait_s = float(remote.get("wait_s", 120)) if hold else 0.0

    hub.control.open_call(pending, wait_s if hold else 0.0)
    hub.broadcast(session_id, "pending", {**pending.as_dict(), "holding": hold})

    # The explanation is requested either way: reading it while you decide is
    # the point, whether you decide in the browser or in the terminal.
    hub.explainer.request(call_id, tool_name, tool_input)
    hub.poke(session_id)

    if not hold:
        return {}

    behavior, updated = hub.control.wait_for(
        pending, wait_s, still_watching=lambda: hub.has_clients(session_id)
    )
    hub.broadcast(session_id, "pending", {**pending.as_dict(), "holding": False})

    if behavior == "allow":
        decision = {"behavior": "allow"}
        if isinstance(updated, dict) and updated:
            decision["updatedInput"] = updated
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
    if behavior == "deny":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny"},
            }
        }
    return {}  # pass: the terminal dialog takes over


def handle_stop(hub: Hub, payload: dict) -> dict:
    session_id = payload.get("session_id") or ""
    hub.poke(session_id)

    queue_cfg = hub.cfg.get("reply_queue") or {}
    if not queue_cfg.get("enabled"):
        return {}

    text = hub.control.take_queued(
        session_id,
        max_chain=int(queue_cfg.get("max_chain", 5)),
        stop_hook_active=bool(payload.get("stop_hook_active")),
    )
    if not text:
        return {}

    live = hub.get(session_id, create=False)
    if live is not None:
        live.note_web_message(text)
        hub.poll_session(live)
    hub.broadcast(session_id, "queue", {"queued": hub.control.queued(session_id), "sent": text})
    return {"decision": "block", "reason": control.format_injection(text)}


# ==================================================================== lifecycle


def _bind_http(preferred: int) -> tuple[Server, int]:
    last = None
    for offset in range(PORT_ATTEMPTS):
        try:
            server = Server(("127.0.0.1", preferred + offset), Handler)
            return server, server.server_address[1]
        except OSError as exc:
            last = exc
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
    raise SystemExit(f"scribe: no free port near {preferred} ({last})")


def _bind_control() -> ControlServer:
    sock_path = paths.control_socket()
    paths.ensure_dirs()
    if sock_path.exists():
        # A stale socket from a killed daemon: probe it, and only remove it if
        # nothing answers. Removing a live one would orphan a running daemon.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.3)
        try:
            probe.connect(str(sock_path))
            probe.close()
            raise SystemExit("scribe: a daemon is already running (scribe stop)")
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError):
            try:
                sock_path.unlink()
            except OSError:
                pass
        finally:
            try:
                probe.close()
            except OSError:
                pass
    server = ControlServer(str(sock_path), ControlHandler)
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass
    return server


def read_record() -> dict:
    return paths.read_json(paths.server_record(), {}) or {}


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def running_record() -> dict | None:
    record = read_record()
    if record and _alive(int(record.get("pid") or 0)):
        return record
    return None


def base_url(record: dict | None = None) -> str:
    record = record or running_record() or {}
    port = record.get("port") or config.load().get("port", 4517)
    return f"http://127.0.0.1:{port}"


def run(port: int | None = None, background: bool = False, open_browser: bool = True) -> int:
    existing = running_record()
    if existing:
        url = base_url(existing)
        sys.stdout.write(f"scribe already running at {url}\n")
        if open_browser:
            webbrowser.open(url)
        return 0

    cfg = config.load()
    paths.ensure_dirs()

    # Bind before forking so the parent can report the real port. Doing it the
    # other way round means printing a URL you only hope is right.
    http, port = _bind_http(int(port or cfg.get("port", 4517)))
    ctrl = _bind_control()
    url = f"http://127.0.0.1:{port}"

    if background:
        sys.stdout.write(f"scribe serving {url}\n")
        sys.stdout.flush()
        if not _daemonize():
            return 0

    hub = Hub(cfg)
    Handler.hub = hub
    ControlHandler.hub = hub

    paths.write_json(
        paths.server_record(),
        {"pid": os.getpid(), "port": port, "url": url, "started": time.time()},
    )

    stop_flag = threading.Event()

    def watch():
        interval = max(0.05, float(cfg.get("watch_interval_ms", 300)) / 1000.0)
        while not stop_flag.is_set():
            try:
                hub.tick()
            except Exception:
                sys.stderr.write("scribe: watcher\n" + traceback.format_exc())
            stop_flag.wait(interval)

    threading.Thread(target=watch, daemon=True, name="scribe-watch").start()
    threading.Thread(target=ctrl.serve_forever, daemon=True, name="scribe-control").start()

    if not background:
        sys.stdout.write(f"scribe serving {url}  (ctrl-c to stop)\n")
        sys.stdout.flush()
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        http.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        sys.stdout.write("\nscribe stopped\n")
    finally:
        stop_flag.set()
        try:
            ctrl.shutdown()
            ctrl.server_close()
        except OSError:
            pass
        try:
            paths.control_socket().unlink()
        except OSError:
            pass
        try:
            paths.server_record().unlink()
        except OSError:
            pass
    return 0


def _daemonize() -> bool:
    """Double-fork. Returns True in the surviving grandchild."""
    if os.name != "posix":
        return True  # no fork available; caller runs in the foreground
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    log = paths.run_dir() / "daemon.log"
    try:
        fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    except OSError:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    return True


def ensure_running(open_browser: bool = False) -> dict | None:
    """Start the daemon if it is not up. Used by the SessionStart hook."""
    record = running_record()
    if record:
        return record
    import subprocess

    entry = Path(__file__).resolve().parent.parent / "bin" / "scribe"
    # Same reasoning as install.hook_command(): the checkout script when it is
    # there, the module entry point when this is an installed wheel.
    argv = (
        [sys.executable, str(entry), "serve", "--background"]
        if entry.is_file()
        else [sys.executable, "-m", "scribe.cli", "serve", "--background"]
    )
    if not open_browser:
        argv.append("--no-browser")
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    for _ in range(40):
        time.sleep(0.05)
        record = running_record()
        if record:
            return record
    return None


def stop() -> int:
    record = running_record()
    if not record:
        sys.stdout.write("scribe is not running\n")
        return 0
    pid = int(record.get("pid") or 0)
    try:
        os.kill(pid, 15)
    except OSError as exc:
        sys.stderr.write(f"scribe: could not stop pid {pid}: {exc}\n")
        return 1
    for _ in range(40):
        time.sleep(0.05)
        if not _alive(pid):
            break
    for path in (paths.server_record(), paths.control_socket()):
        try:
            path.unlink()
        except OSError:
            pass
    sys.stdout.write(f"scribe stopped (pid {pid})\n")
    return 0


def print_status() -> int:
    record = running_record()
    cfg = config.load()
    if record:
        print(f"daemon      running  pid {record.get('pid')}  {record.get('url')}")
    else:
        print("daemon      not running  (scribe serve --background)")
    print(f"logs        {paths.logs_dir()}")
    print(f"config      {paths.config_file()}")
    hooks_state = _hooks_state()
    print(f"hooks       {hooks_state}")
    print(
        "explain     "
        + ("on" if (cfg.get("explain") or {}).get("enabled") else "off")
        + f"  ({(cfg.get('explain') or {}).get('model')}, scope={(cfg.get('explain') or {}).get('scope')})"
    )
    print(
        "approvals   "
        + ("enabled" if (cfg.get("remote_approval") or {}).get("enabled") else "disabled")
    )
    print(
        "replies     " + ("enabled" if (cfg.get("reply_queue") or {}).get("enabled") else "disabled")
    )
    refs = transcript.index_sessions()
    print(f"sessions    {len(refs)} transcripts visible")

    summary = archive.summary()
    print(
        f"archive     {summary['sessions']} sessions, {summary['files']} files, "
        f"{archive.human_bytes(summary['bytes'])}"
    )
    print(f"            {summary['path']}")
    orphans = len(archived_only())
    if orphans:
        print(f"            {orphans} preserved after Claude Code deleted the original")
    return 0


def archived_only() -> list[str]:
    """Session ids that survive only because we archived them."""
    live = {r.session_id for r in transcript.index_sessions()}
    return [p.stem for _, p in archive.iter_archived() if p.stem not in live]


def _hooks_state() -> str:
    try:
        from . import install

        return "installed" if install.is_installed() else "not installed  (scribe install)"
    except Exception:
        return "unknown"


def open_browser_for(session_token: str | None) -> int:
    record = ensure_running(open_browser=False)
    if not record:
        sys.stderr.write("scribe: could not start the daemon\n")
        return 1
    url = record.get("url") or base_url(record)
    from .cli import resolve_ref

    ref = resolve_ref(session_token)
    if ref is not None:
        url = f"{url}/#/s/{ref.session_id}"
    webbrowser.open(url)
    sys.stdout.write(f"{url}\n")
    return 0


def notify_config_changed() -> None:
    """Nudge a running daemon to re-read settings after `scribe config set`."""
    from .hookclient import send

    send({"hook_event_name": "ConfigChanged"}, timeout=0.5)
