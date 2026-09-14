"""Reading Claude Code's JSONL transcripts.

The transcript is our source of truth, not the hook payloads. Hooks tell us
*when* to look; this module decides *what is there*. That split matters because
the documentation is explicit that the transcript "is written asynchronously and
may lag the in-memory conversation" — so a reader that only ever ran on a hook
would miss the tail of the last turn. Here every session is also polled on a
timer, and a lagging write is picked up on the next tick.

Two access patterns:

``TranscriptTail``  incremental, byte-offset based, for live sessions.
``index_sessions``  cheap metadata scan across every project, for the switcher.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from . import paths

# A transcript line can legitimately be large (a Read of a big file), but a
# single line beyond this is corruption or something we have no use for.
MAX_LINE_BYTES = 8 * 1024 * 1024


@dataclass
class TranscriptTail:
    """Incremental line reader that remembers where it stopped.

    Handles the two things that actually go wrong in practice: a partial final
    line (the writer is mid-flush) and truncation/replacement of the file
    (``--resume`` rewrites, or a session id reused after ``/clear``).
    """

    path: Path
    offset: int = 0
    inode: int | None = None
    #: Set when the last :meth:`read_new` restarted from byte zero. Callers that
    #: accumulate rows must drop what they have, or a rewritten transcript
    #: appears twice over.
    restarted: bool = False
    _carry: bytes = b""

    def stat(self) -> os.stat_result | None:
        try:
            return os.stat(self.path)
        except OSError:
            return None

    def reset(self) -> None:
        self.offset = 0
        self.inode = None
        self._carry = b""

    def read_new(self) -> list[dict]:
        """Return rows appended since the last call. Never raises."""
        self.restarted = False
        st = self.stat()
        if st is None:
            return []

        # File replaced or truncated -> start over so we do not read garbage
        # from the middle of a record. `--resume` and compaction can both
        # rewrite a transcript in place.
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.reset()
            self.restarted = True
        self.inode = st.st_ino

        if st.st_size <= self.offset:
            return []

        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read(st.st_size - self.offset)
        except OSError:
            return []

        data = self._carry + chunk
        consumed = len(chunk)
        lines = data.split(b"\n")

        # The last element is whatever follows the final newline: either empty
        # (clean boundary) or a partially written record we must hold back.
        tail = lines.pop()
        if tail:
            if len(tail) > MAX_LINE_BYTES:
                tail = b""  # give up on a runaway line rather than grow forever
            self._carry = tail
        else:
            self._carry = b""

        self.offset += consumed

        rows: list[dict] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


def subagent_dir(transcript_path) -> Path:
    """Where a session's subagent transcripts live.

    Claude Code parks them in a directory named after the session, beside the
    session's own file::

        <project>/<session>.jsonl
        <project>/<session>/subagents/agent-<id>.jsonl
        <project>/<session>/subagents/agent-<id>.meta.json

    The layout is identical inside the archive, so this resolves for both.
    """
    return Path(transcript_path).with_suffix("") / "subagents"


def load_subagents(transcript_path) -> dict:
    """Subagent conversations, keyed by the tool call that spawned each.

    The ``.meta.json`` beside every subagent transcript carries ``toolUseId``,
    which is an exact link back to the ``Task`` call in the parent — far better
    than inferring it from contiguous runs of sidechain rows, which is all the
    main transcript alone would let you do.
    """
    out: dict = {}
    folder = subagent_dir(transcript_path)
    if not folder.is_dir():
        return out
    try:
        files = sorted(folder.glob("agent-*.jsonl"))
    except OSError:
        return out
    for jsonl in files:
        meta = paths.read_json(jsonl.with_suffix(".meta.json"), {}) or {}
        record = {
            "rows": read_all(jsonl),
            "agent_id": jsonl.stem[len("agent-") :],
            "agent_type": str(meta.get("agentType") or ""),
            "description": str(meta.get("description") or ""),
            "depth": meta.get("spawnDepth"),
            "path": str(jsonl),
        }
        if not record["rows"]:
            continue
        tool_use_id = meta.get("toolUseId")
        if tool_use_id:
            out[str(tool_use_id)] = record
        else:
            out.setdefault("", []).append(record)
    return out


def read_all(path) -> list[dict]:
    """Parse an entire transcript. Bad lines are skipped, not fatal."""
    rows: list[dict] = []
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


# ---------------------------------------------------------------- discovery


@dataclass
class SessionRef:
    """What we know about a session without parsing all of it."""

    session_id: str
    path: Path
    cwd: str
    project_dir: str
    title: str = ""
    started: str = ""
    updated: str = ""
    size: int = 0
    mtime: float = 0.0
    git_branch: str = ""
    version: str = ""
    slug: str = ""
    #: True when Claude Code has already deleted the original and this session
    #: exists only because scribe archived it.
    archived: bool = False
    #: Where the transcript's tail says the session stands; see
    #: :func:`scribe.build.turn_state`. Read off the same tail slice as the
    #: title, so the board costs the index nothing extra.
    state: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.session_id,
            "path": str(self.path),
            "cwd": self.cwd,
            "project": paths.project_slug(self.cwd) if self.cwd else self.project_dir,
            "project_dir": self.project_dir,
            "title": self.title,
            "started": self.started,
            "updated": self.updated,
            "size": self.size,
            "mtime": self.mtime,
            "git_branch": self.git_branch,
            "version": self.version,
            "archived": self.archived,
            "state": dict(self.state),
        }


def iter_transcripts(projects_root: Path | None = None) -> Iterator[Path]:
    root = Path(projects_root or paths.projects_dir())
    if not root.is_dir():
        return
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        for jsonl in sorted(project.glob("*.jsonl")):
            yield jsonl


#: peek() results by path, keyed on (size, mtime). The daemon re-indexes every
#: few seconds; without this every rescan re-reads a quarter megabyte from the
#: end of every transcript on the machine to learn nothing has changed.
_PEEK_CACHE: dict[str, tuple[int, int, SessionRef]] = {}

#: How far back to look for a content row when the normal tail slice has none.
#: A row carrying a pasted screenshot runs to a megabyte, and a session that
#: ends on a few of them hides its last real message behind them.
WIDE_TAIL_BYTES = 8 * 1024 * 1024


def peek(path: Path, head_lines: int = 40, tail_bytes: int = 262_144) -> SessionRef:
    """Cheap metadata read: a few lines from the front, a slice from the back.

    A 6 MB transcript takes milliseconds this way, which is what makes an index
    over fifty projects viable on every daemon start. Titles are the interesting
    part — Claude Code writes ``{"type":"ai-title"}`` rows as the conversation
    develops, and the last one is the best summary of the session available.

    Results are cached on (size, mtime), so a transcript that has not changed
    costs one ``stat``. Every caller gets its own copy: the archive sweep sets
    ``archived`` on the ref it is handed, and that must not leak into the cache.
    """
    path = Path(path)
    ref = SessionRef(session_id=path.stem, path=path, cwd="", project_dir=path.parent.name)
    try:
        st = path.stat()
    except OSError:
        return ref
    key = str(path)
    hit = _PEEK_CACHE.get(key)
    if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return replace(hit[2], state=dict(hit[2].state))
    ref = _peek(ref, st, head_lines, tail_bytes)
    _PEEK_CACHE[key] = (st.st_size, st.st_mtime_ns, ref)
    return replace(ref, state=dict(ref.state))


def _tail_rows(fh, size: int, tail_bytes: int) -> list[dict]:
    """Rows from the last ``tail_bytes`` of an open file, first partial line dropped."""
    tail: list[dict] = []
    if size <= 0:
        return tail
    start = max(0, size - tail_bytes)
    fh.seek(start)
    blob = fh.read()
    pieces = blob.split(b"\n")
    if start > 0 and pieces:
        pieces.pop(0)
    for raw in pieces:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(row, dict):
            tail.append(row)
    return tail


def _peek(ref: SessionRef, st: os.stat_result, head_lines: int, tail_bytes: int) -> SessionRef:
    from .build import turn_state  # local import: build imports us back

    path = ref.path
    ref.size = st.st_size
    ref.mtime = st.st_mtime

    head: list[dict] = []
    try:
        with open(path, "rb") as fh:
            for _ in range(head_lines):
                raw = fh.readline()
                if not raw:
                    break
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(row, dict):
                    head.append(row)

            tail = _tail_rows(fh, st.st_size, tail_bytes)
            state_rows = tail or head
            # No content row in the slice, but there is more file behind it:
            # look further back once, for the state only.
            if turn_state(state_rows)["phase"] == "idle" and st.st_size > tail_bytes:
                wide = _tail_rows(fh, st.st_size, min(st.st_size, WIDE_TAIL_BYTES))
                if wide:
                    state_rows = wide
    except OSError:
        return ref

    for row in head + tail:
        if not ref.cwd and row.get("cwd"):
            ref.cwd = row["cwd"]
        if not ref.git_branch and row.get("gitBranch"):
            ref.git_branch = row["gitBranch"]
        if not ref.version and row.get("version"):
            ref.version = row["version"]
        if row.get("sessionId"):
            ref.session_id = row["sessionId"]
        if row.get("slug") and not ref.slug:
            ref.slug = row["slug"]

    stamps = [r.get("timestamp") for r in head if r.get("timestamp")]
    if stamps:
        ref.started = min(stamps)
    stamps = [r.get("timestamp") for r in tail if r.get("timestamp")]
    if stamps:
        ref.updated = max(stamps)

    ref.title = pick_title(head + tail) or _first_prompt_title(head)
    if not ref.title:
        ref.title = ref.slug or "Untitled session"

    ref.state = turn_state(state_rows, ref.cwd)
    return ref


_SLUG_SHAPED = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def is_explainer_cwd(cwd: str, own_root: str = "") -> bool:
    """True for the scratch directory explainer children run in."""
    path = os.path.abspath(os.path.expanduser(cwd or "")).rstrip("/")
    if own_root and path.startswith(own_root):
        return True
    return path.endswith(os.path.join("run", "explain"))


def pick_title(rows: list[dict]) -> str:
    """The best AI-written title in a set of rows.

    Claude Code refines ``ai-title`` as a conversation develops, so the newest is
    normally the best. But when a session runs under a named agent it starts
    writing the *agent's* name into that field instead — a real 1822-row session
    here ends with 30 rows of ``plain-english-tool-call-explanations`` after
    opening with "Add AI explanations for commands in log preview". So: take the
    newest title that is neither a known agent name nor slug-shaped, and only
    fall back to the raw newest if every candidate fails that test.
    """
    agent_names = {
        str(r.get("agentName")).strip() for r in rows if r.get("agentName")
    }
    titles = [
        str(r.get("aiTitle")).strip()
        for r in rows
        if r.get("type") == "ai-title" and r.get("aiTitle")
    ]
    for title in reversed(titles):
        if title in agent_names or _SLUG_SHAPED.match(title):
            continue
        return title
    return titles[0] if titles else ""


def _first_prompt_title(rows: list[dict], limit: int = 72) -> str:
    from .build import user_prompt_text  # local import: build imports us back

    for row in rows:
        if row.get("type") != "user" or row.get("isSidechain"):
            continue
        text = user_prompt_text(row)
        if not text:
            continue
        line = " ".join(text.split())
        return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def index_sessions(
    projects_root: Path | None = None, min_size: int = 200, include_archived: bool = True
) -> list[SessionRef]:
    """Metadata for every transcript, newest first.

    ``min_size`` drops the stubs Claude Code leaves behind for sessions that
    were opened and immediately abandoned; they have no turns and only clutter
    the switcher.
    """
    own = str(paths.root())
    refs = []
    for jsonl in iter_transcripts(projects_root):
        try:
            if jsonl.stat().st_size < min_size:
                continue
        except OSError:
            continue
        ref = peek(jsonl)
        # The explainer runs a real `claude -p`, which gets a transcript of its
        # own. Drop those, or every explanation litters the session list.
        # Matched on the path *shape* rather than the current root: a session
        # archived under an older or relocated SCRIBE_HOME must stay filtered.
        if ref.cwd and is_explainer_cwd(ref.cwd, own):
            continue
        refs.append(ref)

    # Sessions Claude Code has already deleted still exist for us. The archive
    # is not a backup you hope never to need — it is a peer source, and a
    # session that has aged out of ~/.claude/projects must stay readable,
    # searchable and exportable exactly as it was.
    if include_archived:
        seen = {r.session_id for r in refs}
        from . import archive as _archive

        for _project, path in _archive.iter_archived():
            if path.stem in seen:
                continue
            try:
                if path.stat().st_size < min_size:
                    continue
            except OSError:
                continue
            ref = peek(path)
            if ref.cwd and is_explainer_cwd(ref.cwd, own):
                continue
            ref.archived = True
            seen.add(ref.session_id)
            refs.append(ref)

    refs.sort(key=lambda r: r.mtime, reverse=True)
    return refs


def find_transcript(session_id: str, projects_root: Path | None = None) -> Path | None:
    for jsonl in iter_transcripts(projects_root):
        if jsonl.stem == session_id:
            return jsonl
    from . import archive as _archive

    for _project, path in _archive.iter_archived():
        if path.stem == session_id:
            return path
    return None


def transcripts_for_cwd(cwd: str, projects_root: Path | None = None) -> list[SessionRef]:
    target = os.path.abspath(os.path.expanduser(cwd))
    return [r for r in index_sessions(projects_root) if r.cwd and os.path.abspath(r.cwd) == target]
