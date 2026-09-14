"""Transcript rows -> :class:`~scribe.model.Session`.

This is the one builder. Both renderers (markdown for humans, JSON for the
viewer) consume its output, which is what keeps them from drifting apart.

The function is pure and total: it takes the rows it is given and returns a
model, skipping anything it does not recognise rather than raising. New row
types appear in Claude Code releases regularly, and a logger that crashes on an
unfamiliar one is worse than useless.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .model import Notice, Round, Session, Text, Thinking, ToolCall, Usage, tool_kind

# Rows that carry no conversational content. Claude Code writes a lot of these
# for its own bookkeeping and they would swamp a log.
IGNORED_TYPES = {
    "mode",
    "permission-mode",
    "last-prompt",
    "agent-name",
    "attachment",
    "file-history-delta",
    "file-history-snapshot",
    "queue-operation",
    "ai-title",
}

# XML-ish wrappers Claude Code injects around user text.
RE_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
RE_CAVEAT = re.compile(r"<local-command-caveat>.*?</local-command-caveat>\s*", re.S)
RE_TASK_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>\s*", re.S)
RE_HOOK_OUTPUT = re.compile(r"<[a-z-]*hook[a-z-]*>.*?</[a-z-]*hook[a-z-]*>\s*", re.S)
RE_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
RE_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
RE_COMMAND_STDOUT = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)
RE_COMMAND_MESSAGE = re.compile(r"<command-message>.*?</command-message>\s*", re.S)
RE_ANY_TAG_BLOCK = re.compile(
    r"</?(?:command-name|command-args|command-message|local-command-stdout"
    r"|local-command-caveat|system-reminder|task-notification)>",
    re.S,
)

# A message another process put in the session's inbox. Claude Code frames it
# with a header line and a trailing paragraph of guidance for the model, and
# marks the row ``isMeta``. Newer rows also carry ``origin.body``, the text as
# sent; the regexes are the fallback for rows that do not.
RE_PEER_HEADER = re.compile(
    r"\A(?:Another Claude session|A peer session) sent a message(?: while you were working)?:\n"
)
RE_PEER_FOOTER = re.compile(
    r"\n\n(?:This came from another Claude session|That \"other Claude session\")[^\n]*\Z"
)
RE_PEER_ENVELOPE = re.compile(r"\A<cross-session-message(?: [^>]*)?>\n(.*)\n</cross-session-message>\Z", re.S)
#: ``origin.name`` on a message the scribe page sent (see ``peer.SENDER_NAME``).
PAGE_SENDER = "scribe"


# ---------------------------------------------------------------- text helpers


RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_ansi(text: str) -> str:
    """Remove terminal control sequences.

    Plenty of tools colourise when they detect a pipe is a terminal, and Claude
    Code's own command output carries SGR codes. They render as mojibake like
    ``[1mOpus 4.8[22m`` in markdown, so they come out here — once, at build
    time, so both renderers get clean text.
    """
    return RE_ANSI.sub("", text or "")


def _blocks(row: dict) -> list:
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def user_prompt_text(row: dict) -> str:
    """The human-authored part of a user row, wrappers removed.

    Used by the indexer for fallback titles as well as by the builder, hence the
    module-level home.
    """
    peer = peer_message(row)
    if peer is not None:
        return peer[0]
    parts = []
    for block in _blocks(row):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return strip_wrappers("\n".join(parts))[0].strip()


def peer_message(row: dict) -> tuple[str, str] | None:
    """``(text, source)`` for a user row delivered through the session inbox.

    ``source`` is ``web`` when the scribe page sent it and ``peer`` for any
    other sender. ``None`` for every other kind of user row.
    """
    origin = row.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
        return None
    source = "web" if origin.get("name") == PAGE_SENDER else "peer"
    body = origin.get("body")
    if isinstance(body, str):
        return body.strip(), source
    parts = []
    for block in _blocks(row):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    text = "\n".join(parts)
    text = RE_PEER_HEADER.sub("", text)
    text = RE_PEER_FOOTER.sub("", text)
    match = RE_PEER_ENVELOPE.match(text.strip())
    if match:
        text = match.group(1)
    return text.strip(), source


def strip_wrappers(text: str) -> tuple[str, list[str], list[str]]:
    """Split raw user text into (prompt, slash-commands, command-output).

    A ``/effort`` invocation reaches the transcript as XML-ish tags mixed into
    the user message. Dumping that verbatim into a log is unreadable, and
    dropping it entirely loses the fact that you ran a command — so it becomes a
    one-line notice and the real prompt keeps the body.
    """
    if not text:
        return "", [], []

    commands: list[str] = []
    outputs: list[str] = []

    for match in RE_COMMAND_NAME.finditer(text):
        name = (match.group(1) or "").strip()
        if name:
            commands.append(name)
    if commands:
        args = [a.strip() for a in RE_COMMAND_ARGS.findall(text)]
        commands = [
            f"{name} {arg}".strip() for name, arg in zip(commands, args + [""] * len(commands))
        ]
    for match in RE_COMMAND_STDOUT.finditer(text):
        body = (match.group(1) or "").strip()
        if body:
            outputs.append(body)

    cleaned = text
    for pattern in (
        RE_SYSTEM_REMINDER,
        RE_CAVEAT,
        RE_TASK_NOTIFICATION,
        RE_HOOK_OUTPUT,
        RE_COMMAND_MESSAGE,
    ):
        cleaned = pattern.sub("", cleaned)
    cleaned = RE_COMMAND_NAME.sub("", cleaned)
    cleaned = RE_COMMAND_ARGS.sub("", cleaned)
    cleaned = RE_COMMAND_STDOUT.sub("", cleaned)
    cleaned = RE_ANY_TAG_BLOCK.sub("", cleaned)
    return cleaned.strip(), commands, outputs


def _result_to_text(content: Any) -> tuple[str, int]:
    """Flatten a ``tool_result`` body. Returns (text, image_count)."""
    if isinstance(content, str):
        return content, 0
    images = 0
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif block.get("type") == "image":
                images += 1
    return "\n".join(p for p in parts if p), images


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_ms(start: str, end: str) -> int:
    a, b = _parse_ts(start), _parse_ts(end)
    if not a or not b:
        return 0
    delta = int((b - a).total_seconds() * 1000)
    return delta if delta >= 0 else 0


# ---------------------------------------------------------------- subjects


def _rel(path: str, cwd: str) -> str:
    if not path:
        return ""
    try:
        if cwd and os.path.isabs(path):
            rel = os.path.relpath(path, cwd)
            if not rel.startswith(".." + os.sep) and rel != "..":
                return rel
    except ValueError:
        pass
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home) :]
    return path


def _one_line(text: str, limit: int = 110) -> str:
    line = " ".join(str(text or "").split())
    return line[:limit] + ("…" if len(line) > limit else "")


def tool_subject(name: str, tool_input: dict, cwd: str = "") -> str:
    """A one-line answer to "what is this call".

    This is what shows on a collapsed tool card and in the markdown summary, so
    it has to be the most identifying fragment of the call, not just the tool
    name.
    """
    data = tool_input if isinstance(tool_input, dict) else {}

    if name in ("Bash", "BashOutput", "KillShell"):
        return _one_line(data.get("command") or data.get("description") or "")
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead"):
        return _rel(data.get("file_path") or data.get("notebook_path") or "", cwd)
    if name in ("Glob", "Grep"):
        pattern = data.get("pattern") or ""
        where = _rel(data.get("path") or "", cwd)
        return _one_line(f"{pattern}  in {where}" if where else pattern)
    if name in ("Agent", "Task"):
        return _one_line(data.get("description") or data.get("subagent_type") or "subagent")
    if name == "Skill":
        return _one_line(data.get("skill") or "")
    if name == "WebFetch":
        return _one_line(data.get("url") or "")
    if name in ("WebSearch", "ToolSearch"):
        return _one_line(data.get("query") or "")
    if name == "TodoWrite":
        todos = data.get("todos")
        return f"{len(todos)} items" if isinstance(todos, list) else "todo list"
    if name in ("TaskCreate", "TaskUpdate"):
        return _one_line(data.get("subject") or data.get("taskId") or "")
    if name == "AskUserQuestion":
        questions = data.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            if isinstance(first, dict):
                return _one_line(first.get("question") or first.get("header") or "")
        return "question"
    if name.startswith("mcp__"):
        return name
    # Unknown tool: show the first scalar argument, which is usually the subject.
    for key in ("path", "file", "url", "query", "name", "prompt", "description"):
        if isinstance(data.get(key), str) and data[key].strip():
            return _one_line(data[key])
    return ""


# ---------------------------------------------------------------- turn state

#: Tools whose call is a question to the person, not work. A pending one means
#: the session is blocked on them, not on the model.
ASKS = ("AskUserQuestion", "ExitPlanMode")

#: What Claude Code writes as the user text when a turn is cut short.
INTERRUPTED = "[Request interrupted"


def turn_state(rows, cwd: str = "") -> dict:
    """Where a session stands right now, read off the tail of its transcript.

    This is what puts a session in a column on the board, and it is a function
    of the rows alone — the same rule as the rest of the model. Hooks make the
    answer arrive sooner; they never change it. Only the last content row
    matters, so the scan walks backwards and stops early, which keeps it cheap
    enough to run on every ``peek`` and every poll.

    ``phase`` is one of:

    ``needs_you``  the last thing Claude did was ask — a question, or a plan
                   waiting to be approved.
    ``working``    a tool is running, or Claude is mid-reply.
    ``your_turn``  Claude ended its turn (``stop_reason: end_turn``) or the
                   person interrupted it; either way the next move is theirs.
    ``idle``       no content rows at all.

    Whether the process behind the transcript is still alive is not knowable
    from the file, so ``done`` is decided by the daemon, not here.
    """
    state = {
        "phase": "idle",
        "mode": "",
        "activity": "",
        "activity_kind": "",
        "reply": "",
        "since": "",
        "turn_started": "",
        "tool": "",
    }
    rows = [r for r in rows if isinstance(r, dict)]

    for row in reversed(rows):
        if row.get("type") == "permission-mode" and row.get("permissionMode"):
            state["mode"] = str(row["permissionMode"])
            break

    decided = False
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        kind = row.get("type")
        if kind not in ("user", "assistant") or row.get("isSidechain"):
            continue
        blocks = [b for b in _blocks(row) if isinstance(b, dict)]

        if kind == "user":
            if row.get("isMeta") and peer_message(row) is None:
                continue
            if any(b.get("type") == "tool_result" for b in blocks):
                if not decided:
                    if any(INTERRUPTED in str(b.get("content") or "")[:80] for b in blocks):
                        state.update(phase="your_turn", activity="interrupted", activity_kind="stop")
                    else:
                        state.update(phase="working", activity="thinking", activity_kind="wait")
                    state["since"] = row.get("timestamp") or ""
                    decided = True
                continue
            text = user_prompt_text(row)
            if not text and not any(b.get("type") == "image" for b in blocks):
                continue
            if not decided:
                if text.startswith(INTERRUPTED):
                    state.update(phase="your_turn", activity="interrupted", activity_kind="stop")
                else:
                    state.update(phase="working", activity="reading the prompt", activity_kind="wait")
                state["since"] = row.get("timestamp") or ""
                decided = True
            if not text.startswith(INTERRUPTED):
                state["turn_started"] = row.get("timestamp") or ""
                break
            continue

        # assistant
        if decided:
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        stop = str(message.get("stop_reason") or "")
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        state["since"] = row.get("timestamp") or ""
        if calls:
            call = calls[-1]
            name = str(call.get("name") or "Tool")
            subject = tool_subject(name, call.get("input") or {}, cwd)
            if name in ASKS:
                state.update(
                    phase="needs_you",
                    activity=subject or ("plan ready" if name == "ExitPlanMode" else "question"),
                    activity_kind="plan" if name == "ExitPlanMode" else "ask",
                )
            else:
                state.update(phase="working", activity=subject or name, activity_kind=tool_kind(name))
                state["tool"] = name
        elif (stop and stop != "tool_use") or (not stop and any(b.get("type") == "text" for b in blocks)):
            # `end_turn` is the clean signal. Older transcripts omit the stop
            # reason; there a final text block is taken as the end of the turn,
            # because a tool call would have followed it within a second.
            state.update(phase="your_turn", activity="replied", activity_kind="reply")
            state["reply"] = _last_text(rows, i)
        else:
            state.update(phase="working", activity="thinking", activity_kind="wait")
        decided = True

    return state


def _last_text(rows: list[dict], end: int, limit: int = 160) -> str:
    """First line of the final text block of the message ending at ``end``."""
    request = rows[end].get("requestId") or (rows[end].get("message") or {}).get("id") or ""
    for j in range(end, -1, -1):
        row = rows[j]
        if row.get("type") != "assistant":
            break
        same = (row.get("requestId") or (row.get("message") or {}).get("id") or "") == request
        if request and not same:
            break
        for block in reversed(_blocks(row)):
            if isinstance(block, dict) and block.get("type") == "text" and (block.get("text") or "").strip():
                first = next((ln for ln in block["text"].splitlines() if ln.strip()), "")
                return _one_line(first.replace("**", "").replace("`", "").lstrip("#>- ").strip(), limit)
    return ""


# ---------------------------------------------------------------- the builder


class _RoundBuilder:
    def __init__(self, session: Session):
        self.session = session
        self.rounds: list[Round] = []
        self.current: Round | None = None
        self.calls: dict[str, ToolCall] = {}
        self.pending_notices: list[Notice] = []
        self.last_ts: str = ""

    def open_round(self, ts: str, uuid: str, prompt: str, source: str = "user") -> Round:
        rnd = Round(index=len(self.rounds) + 1, uuid=uuid, ts=ts, prompt=prompt, source=source)
        for notice in self.pending_notices:
            rnd.items.append(notice)
        self.pending_notices.clear()
        self.rounds.append(rnd)
        self.current = rnd
        return rnd

    def ensure_round(self, ts: str) -> Round:
        """Assistant output before any user prompt (a resumed session, a hook
        injecting an initial message) still needs somewhere to live."""
        if self.current is None:
            self.open_round(ts, uuid="", prompt="", source="system")
        return self.current

    def add(self, item) -> None:
        self.ensure_round(item.ts).items.append(item)

    def add_notice(self, text: str, ts: str, variant: str = "info") -> None:
        notice = Notice(ts=ts, text=text, variant=variant)
        if self.current is None:
            self.pending_notices.append(notice)
        else:
            self.current.items.append(notice)

    def last_seen_ts(self) -> str:
        """Timestamp of the most recent item, for estimating think time."""
        if self.current and self.current.items:
            return self.current.items[-1].ts or self.current.ts
        if self.current:
            return self.current.ts
        return self.last_ts


def build(
    rows: Iterable[dict],
    transcript_path: str = "",
    cwd_hint: str = "",
    subagents: dict | None = None,
    nested: bool = False,
) -> Session:
    rows = [r for r in rows if isinstance(r, dict)]
    session = Session(transcript_path=str(transcript_path or ""))

    # Every row in a subagent's own transcript carries `isSidechain: true` —
    # it is a sidechain *of the parent*, but it is the whole conversation at
    # this level. Splitting on the flag here would leave no main rows at all.
    if nested:
        main_rows, side_rows = rows, []
    else:
        main_rows = [r for r in rows if not r.get("isSidechain")]
        side_rows = [r for r in rows if r.get("isSidechain")]

    # ---- session-level metadata (last writer wins for things that change)
    for row in rows:
        if row.get("sessionId"):
            session.id = row["sessionId"]
        elif row.get("session_id"):
            session.id = row["session_id"]
        if row.get("cwd"):
            session.cwd = row["cwd"]
        if row.get("gitBranch"):
            session.git_branch = row["gitBranch"]
        if row.get("version"):
            session.version = row["version"]
        if row.get("slug"):
            session.slug = row["slug"]
    from .transcript import pick_title

    session.title = pick_title(rows)
    if not session.cwd:
        session.cwd = cwd_hint or ""
    if not session.id and transcript_path:
        session.id = os.path.basename(str(transcript_path)).rsplit(".", 1)[0]

    cwd = session.cwd
    builder = _RoundBuilder(session)

    for row in main_rows:
        rtype = row.get("type")
        ts = row.get("timestamp") or ""
        if ts:
            builder.last_ts = ts

        if rtype in IGNORED_TYPES:
            continue

        if rtype == "system":
            _handle_system(builder, row, ts)
            continue

        if rtype == "user":
            _handle_user(builder, row, ts, cwd)
            continue

        if rtype == "assistant":
            _handle_assistant(builder, row, ts, session)
            continue

    _attach_subagent_files(builder, subagents or {}, cwd)
    _attach_sidechains(builder, side_rows, cwd)
    _finalize(builder, session)
    return session


def _handle_system(builder: _RoundBuilder, row: dict, ts: str) -> None:
    subtype = row.get("subtype")
    if subtype == "compact_boundary":
        builder.add_notice("Context compacted — earlier messages summarised", ts, "compact")
    elif subtype == "turn_duration":
        if builder.current is not None:
            try:
                builder.current.duration_ms = max(
                    builder.current.duration_ms, int(row.get("durationMs") or 0)
                )
            except (TypeError, ValueError):
                pass
    elif subtype == "away_summary":
        return
    else:
        # `subtype: local_command` rows carry the same XML-ish wrappers as user
        # rows do, so they get the same treatment rather than being dumped raw.
        content = row.get("content")
        if not isinstance(content, str) or not content.strip() or row.get("isMeta"):
            return
        text, commands, outputs = strip_wrappers(content)
        for command in commands:
            builder.add_notice(f"/{command.lstrip('/')}", ts, "command")
        for out in outputs:
            builder.add_notice(_one_line(_strip_ansi(out), 300), ts, "info")
        if text:
            builder.add_notice(_one_line(_strip_ansi(text), 200), ts, "info")


def _handle_user(builder: _RoundBuilder, row: dict, ts: str, cwd: str) -> None:
    blocks = _blocks(row)

    # Tool results ride on user rows; they belong to an existing call, not to a
    # new round. The structured sidecar (stdout/stderr/patch) sits at top level.
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    if results:
        sidecar = row.get("toolUseResult")
        for block in results:
            call = builder.calls.get(block.get("tool_use_id") or "")
            if call is None:
                continue
            _apply_result(call, block, sidecar, ts)
        return

    # A message from the inbox is marked meta, but it is a prompt: someone
    # wrote it and Claude answers it. Skipping it would show a reply to nothing.
    peer = peer_message(row)
    if peer is not None:
        text, source = peer
        if text:
            builder.open_round(ts, row.get("uuid") or "", text, source=source)
        return

    if row.get("isMeta"):
        return

    images = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image")
    raw = "\n".join(
        b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    prompt, commands, outputs = strip_wrappers(raw)

    if not prompt and not commands and not outputs and not images:
        return

    if prompt or images:
        rnd = builder.open_round(ts, row.get("uuid") or "", prompt, source="user")
        rnd.images = images
        for command in commands:
            rnd.items.append(Notice(ts=ts, text=f"/{command.lstrip('/')}", variant="command"))
        for out in outputs:
            rnd.items.append(Notice(ts=ts, text=_one_line(_strip_ansi(out), 300), variant="info"))
        return

    # A bare slash command with no prompt of its own: keep it as a notice so it
    # lands at the top of whatever round follows.
    for command in commands:
        builder.add_notice(f"/{command.lstrip('/')}", ts, "command")
    for out in outputs:
        builder.add_notice(_one_line(_strip_ansi(out), 300), ts, "info")


def _handle_assistant(builder: _RoundBuilder, row: dict, ts: str, session: Session) -> None:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    model = message.get("model")
    if model and model not in session.models:
        session.models.append(model)

    usage = Usage.from_raw(message.get("usage"))
    session.usage.add(usage)
    rnd = builder.ensure_round(ts)
    rnd.usage.add(usage)

    for block in _blocks(row):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                builder.add(Text(uuid=row.get("uuid") or "", ts=ts, md=text))

        elif btype == "thinking":
            text = (block.get("thinking") or "").strip()
            if not text:
                continue  # redacted/empty thinking carries only a signature
            seconds = _elapsed_ms(builder.last_seen_ts(), ts) / 1000.0 if builder.last_ts else 0.0
            builder.add(Thinking(uuid=row.get("uuid") or "", ts=ts, md=text, seconds=seconds))

        elif btype == "tool_use":
            name = block.get("name") or "Tool"
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            call = ToolCall(
                uuid=row.get("uuid") or "",
                ts=ts,
                id=block.get("id") or "",
                name=name,
                tool_kind=tool_kind(name),
                input=tool_input,
                subject=tool_subject(name, tool_input, session.cwd),
            )
            builder.calls[call.id] = call
            builder.add(call)


def _apply_result(call: ToolCall, block: dict, sidecar: Any, ts: str) -> None:
    text, images = _result_to_text(block.get("content"))
    call.result_text = _strip_ansi(text)
    call.result_images = images
    call.duration_ms = _elapsed_ms(call.ts, ts)
    call.status = "error" if block.get("is_error") else "ok"

    if not isinstance(sidecar, dict):
        return

    if "stdout" in sidecar or "stderr" in sidecar:
        call.stdout = _strip_ansi(str(sidecar.get("stdout") or ""))
        call.stderr = _strip_ansi(str(sidecar.get("stderr") or ""))
        if sidecar.get("interrupted"):
            call.status = "interrupted"
    if sidecar.get("structuredPatch"):
        call.patch = sidecar["structuredPatch"]
    for key in ("filePath", "file_path"):
        if sidecar.get(key):
            call.file_path = str(sidecar[key])
            break
    if isinstance(sidecar.get("file"), dict) and sidecar["file"].get("filePath"):
        call.file_path = str(sidecar["file"]["filePath"])
    if sidecar.get("oldString") is not None:
        call.old_string = str(sidecar.get("oldString") or "")
        call.new_string = str(sidecar.get("newString") or "")


def _attach_subagent_files(builder: _RoundBuilder, subagents: dict, cwd: str) -> None:
    """Nest subagent conversations recorded in their own files.

    Exact, not inferred: each subagent's ``.meta.json`` names the ``toolUseId``
    that spawned it, so the conversation lands under precisely the right Task
    call even when several ran in parallel.
    """
    for tool_use_id, record in (subagents or {}).items():
        if not tool_use_id or not isinstance(record, dict):
            continue
        call = builder.calls.get(tool_use_id)
        if call is None:
            continue
        conversation = build(record.get("rows") or [], cwd_hint=cwd, nested=True)
        call.subagent = conversation.rounds
        call.agent_name = record.get("agent_type") or record.get("description") or call.agent_name


def _attach_sidechains(builder: _RoundBuilder, side_rows: list[dict], cwd: str) -> None:
    """Nest subagent rows that live inline in the parent transcript.

    Older Claude Code versions interleaved sidechain rows into the main file
    with no link back to the spawning ``tool_use``, so each contiguous run is
    attached to the most recent unfilled task call — correct for sequential
    subagents, a reasonable guess for parallel ones. Newer versions write
    separate files instead; see :func:`_attach_subagent_files`.
    """
    if not side_rows:
        return
    task_calls = [
        c for c in builder.calls.values() if c.tool_kind == "task" and not c.subagent
    ]
    if not task_calls:
        return
    task_calls.sort(key=lambda c: c.ts)

    runs: list[list[dict]] = []
    current: list[dict] = []
    prev_uuid = None
    for row in side_rows:
        if current and row.get("parentUuid") not in (prev_uuid, None):
            runs.append(current)
            current = []
        current.append(row)
        prev_uuid = row.get("uuid")
    if current:
        runs.append(current)

    for call, run in zip(task_calls, runs):
        conversation = build(run, cwd_hint=cwd, nested=True)
        call.subagent = conversation.rounds
        for row in run:
            if row.get("agentName"):
                call.agent_name = str(row["agentName"])
                break


def _finalize(builder: _RoundBuilder, session: Session) -> None:
    rounds = builder.rounds
    session.rounds = rounds

    for i, rnd in enumerate(rounds):
        last = rnd.ts
        for item in rnd.items:
            if item.ts and item.ts > last:
                last = item.ts
        rnd.end_ts = last
        if not rnd.duration_ms:
            rnd.duration_ms = _elapsed_ms(rnd.ts, last)

    # A call with no result either was denied, or is still running because it is
    # the very last thing in the transcript. Only the final round can be live.
    for i, rnd in enumerate(rounds):
        is_last = i == len(rounds) - 1
        for call in rnd.tool_calls:
            if call.status == "pending" and not is_last:
                call.status = "no-result"
        _mark_subagent_rounds(rnd)

    if rounds:
        session.started = session.started or rounds[0].ts
        session.updated = rounds[-1].end_ts or rounds[-1].ts
    if not session.title:
        session.title = _fallback_title(rounds)


def _mark_subagent_rounds(rnd: Round) -> None:
    for call in rnd.tool_calls:
        for sub in call.subagent:
            for nested in sub.tool_calls:
                if nested.status == "pending":
                    nested.status = "no-result"


def _fallback_title(rounds: list[Round], limit: int = 72) -> str:
    for rnd in rounds:
        if rnd.prompt:
            line = " ".join(rnd.prompt.split())
            return line[:limit] + ("…" if len(line) > limit else "")
    return "Untitled session"


def build_from_path(path, cwd_hint: str = "") -> Session:
    from .transcript import load_subagents, read_all

    return build(
        read_all(path),
        transcript_path=str(path),
        cwd_hint=cwd_hint,
        subagents=load_subagents(path),
    )
