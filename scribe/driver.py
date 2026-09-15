"""A headless Claude Code child the page drives.

The inbox socket reaches a session someone is running in a terminal, and
only carries text. A session with no process behind it needs a process, and
the page wants more than text from it: pictures, a permission mode, a model,
a stop button, the list of skills it can invoke. All of that exists in Claude
Code's own headless protocol, so the driver is that protocol and nothing
else::

    claude -p --input-format stream-json --output-format stream-json \\
           --permission-prompt-tool stdio [--resume <id> | --session-id <id>]

One child per driven session, kept alive between turns and closed after
``driver.idle_min`` of silence; the next message starts it again with
``--resume`` on the same id, so the transcript is one file throughout. The
driver only *writes into* a session. Everything shown on the page still comes
from the transcript, exactly as it does for a terminal session: this module
never renders.

What Claude Code 2.1.272 actually does on that wire (checked, not read):

* ``system/init`` is emitted at the start of **every** turn, not at startup:
  ``model``, ``permissionMode``, ``slash_commands`` (user-invocable),
  ``terminal_slash_commands`` (TUI-only), ``skills``, ``agents``, ``tools``.
* stdin stays open across turns. Each user frame is one turn ending in a
  ``result`` frame (``subtype: success`` or ``error_during_execution`` after
  an interrupt).
* ``control_request`` frames from us: ``initialize`` (answered with the full
  command catalogue, with descriptions), ``set_permission_mode`` (answered
  with the mode now in effect, followed by a ``system/status`` frame),
  ``set_model`` (writes a ``<local-command-stdout>`` row), ``interrupt``,
  ``get_context_usage``, ``file_suggestions``.
* ``control_request`` frames from the child: ``can_use_tool`` with
  ``tool_name``, ``input``, ``tool_use_id``, ``description``,
  ``permission_suggestions``, ``blocked_path``. Only with
  ``--permission-prompt-tool stdio``; with the default flags a prompt is
  answered by a local deny and never reaches the host.
* A user frame may carry ``image`` content blocks (base64), and the row lands
  in the transcript with them.
* A mode change does not write a ``permission-mode`` row; the next user row
  carries ``permissionMode`` instead. ``build.turn_state`` reads both.
* The child registers an inbox of its own (``messaging_socket_path`` in
  init). The daemon must prefer the driver over that inbox for a driven
  session, or it would message its own child through the side door.
* ``--bare`` is never passed: it reads auth only from ``ANTHROPIC_API_KEY``,
  never the keychain, which breaks subscription users (same as the explainer).
"""

from __future__ import annotations

import base64
import collections
import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import peer

#: How long a control request may take. Claude Code answers most of them in
#: milliseconds; ``get_context_usage`` with ``detail: full`` counts tokens.
REQUEST_TIMEOUT_S = 30.0

#: Image types Claude Code accepts as content blocks.
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

#: Modes the page may pick. ``default`` is what the CLI calls ``manual``.
MODES = ("default", "acceptEdits", "plan", "auto", "bypassPermissions")

#: Model aliases Claude Code resolves itself. ``default`` clears an override.
MODELS = ("default", "fable", "opus", "sonnet", "haiku")


class DriverError(Exception):
    pass


@dataclass
class Caps:
    """What the child said it can do, from ``initialize`` and ``system/init``."""

    model: str = ""
    mode: str = ""
    commands: list = field(default_factory=list)  # [{name, description, argument_hint}]
    slash_commands: list = field(default_factory=list)  # names, user-invocable
    terminal_commands: list = field(default_factory=list)  # names, TUI-only
    skills: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    tools: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "mode": self.mode,
            "commands": list(self.commands),
            "slash_commands": list(self.slash_commands),
            "terminal_commands": list(self.terminal_commands),
            "skills": list(self.skills),
            "agents": list(self.agents),
        }


def claude_binary() -> str | None:
    """The binary to drive. ``SCRIBE_CLAUDE`` wins, for tests and odd installs."""
    override = os.environ.get("SCRIBE_CLAUDE")
    if override:
        return override
    from .explain import find_claude

    return find_claude()


def image_block(path: str, media_type: str) -> dict | None:
    if media_type not in IMAGE_TYPES:
        return None
    try:
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


class Driver:
    """One headless child, one session.

    ``on_event(driver, kind, data)`` is called on the reader thread for
    ``init``, ``turn``, ``result``, ``mode`` and ``exit``; keep it quick.
    ``on_permission(driver, request)`` is called on a thread of its own and
    may block; it returns ``{"behavior": "allow", "updatedInput": ...}`` or
    ``{"behavior": "deny", "message": ...}``.
    """

    def __init__(
        self,
        session_id: str,
        cwd: str,
        *,
        resume: bool,
        mode: str = "",
        model: str = "",
        on_event=None,
        on_permission=None,
        claude: str | None = None,
        env: dict | None = None,
    ):
        self.session_id = session_id
        self.cwd = cwd
        self.resume = resume
        self.mode = mode
        self.model = model
        self.on_event = on_event
        self.on_permission = on_permission
        self.claude = claude
        self.env = env

        self.state = "starting"  # starting | idle | running | exited
        self.caps = Caps(mode=mode, model=model)
        self.started_at = time.time()
        self.idle_since = time.time()
        self.turn_started = 0.0
        self.turns = 0
        self.last_result: dict = {}
        self.error = ""
        self.exit_code: int | None = None

        self.proc: subprocess.Popen | None = None
        self._queue: collections.deque[dict] = collections.deque()
        self._waiting: dict[str, tuple[threading.Event, list]] = {}
        self._stderr: collections.deque[str] = collections.deque(maxlen=40)
        self._wlock = threading.Lock()
        self._lock = threading.RLock()
        self._exited = threading.Event()

    # -- lifecycle -------------------------------------------------------

    def argv(self) -> list[str]:
        binary = self.claude or claude_binary()
        if not binary:
            raise DriverError("claude is not on PATH")
        argv = [
            binary,
            "-p",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--permission-prompt-tool",
            "stdio",
        ]
        argv += ["--resume", self.session_id] if self.resume else ["--session-id", self.session_id]
        if self.mode:
            argv += ["--permission-mode", "manual" if self.mode == "default" else self.mode]
        if self.model and self.model != "default":
            argv += ["--model", self.model]
        return argv

    def start(self) -> "Driver":
        if self.cwd and not os.path.isdir(self.cwd):
            raise DriverError(f"the session's directory is gone: {self.cwd}")
        argv = self.argv()
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=self.cwd or None,
                env=self.env if self.env is not None else peer.child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise DriverError(f"could not start claude: {exc}") from exc
        self._reader = threading.Thread(target=self._read, daemon=True, name=f"scribe-driver-{self.session_id[:8]}")
        self._reader.start()
        self._errs = threading.Thread(target=self._drain_stderr, daemon=True)
        self._errs.start()
        # `initialize` registers us as the host and returns the catalogue.
        try:
            reply = self.request("initialize", timeout=15.0)
        except DriverError as exc:
            self.stop()
            raise DriverError(f"claude did not answer: {exc}") from exc
        self._absorb_initialize(reply)
        with self._lock:
            if self.state == "starting":
                self.state = "idle"
                self.idle_since = time.time()
        return self

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self._exited.is_set()

    def idle_for(self) -> float:
        with self._lock:
            if self.state != "idle":
                return 0.0
            return time.time() - self.idle_since

    def stop(self, grace: float = 5.0) -> None:
        """Close stdin and let the child finish; kill it if it will not."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._mark_exited()
        for thread in (getattr(self, "_reader", None), getattr(self, "_errs", None)):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    # -- sending -----------------------------------------------------------

    def send(self, text: str, images: list[dict] | None = None) -> dict:
        """Queue or write one user turn. Returns ``{"ok", "queued"}``."""
        text = (text or "").rstrip()
        if not text and not images:
            return {"ok": False, "error": "empty message"}
        if not self.alive:
            return {"ok": False, "error": self.error or "claude has exited"}
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        content.extend(images or [])
        frame = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": self.session_id,
        }
        with self._lock:
            if self.state == "running":
                self._queue.append({"text": text, "frame": frame})
                return {"ok": True, "queued": True, "depth": len(self._queue)}
            self._begin_turn()
        self._write(frame)
        return {"ok": True, "queued": False}

    def queued(self) -> list[str]:
        with self._lock:
            return [q["text"] for q in self._queue]

    def drop_queued(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self._queue):
                del self._queue[index]
                return True
        return False

    def request(self, subtype: str, timeout: float = REQUEST_TIMEOUT_S, **fields) -> dict:
        """One control request, answered or raised."""
        if not self.alive:
            raise DriverError(self.error or "claude has exited")
        rid = uuid.uuid4().hex[:12]
        done = threading.Event()
        slot: list = []
        with self._lock:
            self._waiting[rid] = (done, slot)
        self._write({"type": "control_request", "request_id": rid, "request": dict(subtype=subtype, **fields)})
        if not done.wait(timeout):
            with self._lock:
                self._waiting.pop(rid, None)
            raise DriverError(f"{subtype}: no answer in {timeout:.0f}s")
        reply = slot[0] if slot else {}
        if reply.get("subtype") == "error":
            raise DriverError(str(reply.get("error") or f"{subtype} failed"))
        return reply.get("response") or {}

    def interrupt(self) -> dict:
        return self.request("interrupt")

    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise DriverError(f"unknown permission mode: {mode}")
        reply = self.request("set_permission_mode", mode=mode)
        now = str(reply.get("mode") or mode)
        with self._lock:
            self.caps.mode = now
            self.mode = now
        return now

    def set_model(self, model: str) -> str:
        self.request("set_model", model=None if model in ("", "default") else model)
        with self._lock:
            self.model = model
            self.caps.model = "" if model == "default" else model
        return model

    def context_usage(self) -> dict:
        return self.request("get_context_usage")

    def file_suggestions(self, query: str) -> list[dict]:
        reply = self.request("file_suggestions", timeout=10.0, query=query)
        return list(reply.get("suggestions") or [])

    # -- the wire ----------------------------------------------------------

    def _write(self, frame: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise DriverError("claude has exited")
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        with self._wlock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.error = self.error or f"claude closed its input: {exc}"
                self._mark_exited()
                raise DriverError(self.error) from exc

    def _read(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    try:
                        self._on_frame(frame)
                    except Exception:  # a bad frame must not stop the reader
                        pass
        finally:
            self._mark_exited()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                self._stderr.append(line[:400])

    def _on_frame(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "control_response":
            reply = frame.get("response") or {}
            rid = str(reply.get("request_id") or "")
            with self._lock:
                waiter = self._waiting.pop(rid, None)
            if waiter:
                done, slot = waiter
                slot.append(reply)
                done.set()
        elif kind == "control_request":
            request = frame.get("request") or {}
            rid = str(frame.get("request_id") or "")
            if request.get("subtype") == "can_use_tool":
                threading.Thread(
                    target=self._answer_permission, args=(rid, request), daemon=True
                ).start()
            else:
                self._write(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": rid,
                            "error": f"scribe does not handle {request.get('subtype')}",
                        },
                    }
                )
        elif kind == "system":
            sub = frame.get("subtype")
            if sub == "init":
                self._absorb_init(frame)
                self._emit("init", self.caps.as_dict())
            elif sub == "status" and frame.get("permissionMode"):
                with self._lock:
                    self.caps.mode = str(frame["permissionMode"])
                    self.mode = self.caps.mode
                self._emit("mode", {"mode": self.caps.mode})
        elif kind == "result":
            self._end_turn(frame)

    def _answer_permission(self, rid: str, request: dict) -> None:
        decision: dict = {"behavior": "deny", "message": "scribe: nobody answered"}
        if self.on_permission is not None:
            try:
                got = self.on_permission(self, request)
                if isinstance(got, dict) and got.get("behavior") in ("allow", "deny"):
                    decision = got
            except Exception as exc:
                decision = {"behavior": "deny", "message": f"scribe: {exc}"[:200]}
        if decision.get("behavior") == "allow" and "updatedInput" not in decision:
            decision = dict(decision, updatedInput=request.get("input") or {})
        try:
            self._write(
                {"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": decision}}
            )
        except DriverError:
            pass

    def _absorb_initialize(self, reply: dict) -> None:
        commands = []
        for item in reply.get("commands") or []:
            if isinstance(item, dict) and item.get("name"):
                commands.append(
                    {
                        "name": str(item["name"]),
                        "description": str(item.get("description") or ""),
                        "argument_hint": str(item.get("argumentHint") or item.get("argument_hint") or ""),
                    }
                )
        with self._lock:
            if commands:
                self.caps.commands = commands
                if not self.caps.slash_commands:
                    self.caps.slash_commands = [c["name"] for c in commands]
            for key in ("model", "output_style"):
                if reply.get(key) and key == "model":
                    self.caps.model = str(reply[key])

    def _absorb_init(self, frame: dict) -> None:
        with self._lock:
            caps = self.caps
            if frame.get("model"):
                caps.model = str(frame["model"])
            if frame.get("permissionMode"):
                caps.mode = str(frame["permissionMode"])
                self.mode = caps.mode
            for src, dst in (
                ("slash_commands", "slash_commands"),
                ("terminal_slash_commands", "terminal_commands"),
                ("skills", "skills"),
                ("agents", "agents"),
                ("tools", "tools"),
            ):
                if isinstance(frame.get(src), list):
                    setattr(caps, dst, [str(x) for x in frame[src]])

    def _begin_turn(self) -> None:
        # Caller holds the lock.
        self.state = "running"
        self.turn_started = time.time()
        self.turns += 1

    def _end_turn(self, result: dict) -> None:
        nxt = None
        with self._lock:
            self.last_result = {
                "subtype": str(result.get("subtype") or ""),
                "is_error": bool(result.get("is_error")),
                "duration_ms": result.get("duration_ms"),
                "cost_usd": result.get("total_cost_usd"),
            }
            if self._queue:
                nxt = self._queue.popleft()
                self._begin_turn()
            else:
                self.state = "idle"
                self.idle_since = time.time()
        self._emit("result", dict(self.last_result, queued=len(self._queue)))
        if nxt is not None:
            try:
                self._write(nxt["frame"])
            except DriverError:
                pass
            self._emit("turn", {"text": nxt["text"], "queued": len(self._queue)})

    def _mark_exited(self) -> None:
        if self._exited.is_set():
            return
        self._exited.set()
        with self._lock:
            self.state = "exited"
            if self.proc is not None:
                self.exit_code = self.proc.poll()
            if not self.error and self._stderr and (self.exit_code or 0) != 0:
                self.error = self._stderr[-1]
            waiting = list(self._waiting.values())
            self._waiting.clear()
        for done, slot in waiting:
            slot.append({"subtype": "error", "error": self.error or "claude exited"})
            done.set()
        self._emit("exit", {"code": self.exit_code, "error": self.error})

    def _emit(self, kind: str, data: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(self, kind, data)
        except Exception:
            pass

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "model": self.caps.model or self.model,
                "mode": self.caps.mode or self.mode,
                "idle_since": self.idle_since if self.state == "idle" else 0,
                "turn_started": self.turn_started if self.state == "running" else 0,
                "turns": self.turns,
                "queued": len(self._queue),
                "error": self.error,
            }
