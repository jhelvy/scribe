"""Session -> CommonMark.

The markdown file is the artifact you keep: greppable, diffable, readable in any
editor, and readable in ten years when this program no longer exists. Nothing
parses it back — the viewer reads structured JSON from the daemon — so unlike
the previous generation of this tool it carries no marker comments and no
machine-readable scaffolding. It is free to be a document.

Editorial line: record enough to trace what happened, not everything that
crossed the wire. Bash output is worth keeping; the 2,000 lines of a file that
was merely *read* are not, since the file itself is still on disk.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from .model import Notice, Round, Session, Text, Thinking, ToolCall

GAP_MINUTES = 30


# ---------------------------------------------------------------- formatting


def fence_for(text: str) -> str:
    """A fence long enough to survive backticks inside the content."""
    longest = 0
    for run in re.findall(r"`+", text or ""):
        longest = max(longest, len(run))
    return "`" * max(3, longest + 1)


def code_block(text: str, lang: str = "") -> str:
    fence = fence_for(text)
    body = (text or "").rstrip("\n")
    return f"{fence}{lang}\n{body}\n{fence}"


def clip(text: str, limit: int) -> tuple[str, int]:
    """Truncate on a line boundary. Returns (text, lines_dropped)."""
    if not text or limit <= 0 or len(text) <= limit:
        return text or "", 0
    head = text[:limit]
    cut = head.rfind("\n")
    if cut > limit // 2:
        head = head[:cut]
    dropped = text[len(head) :].count("\n") + 1
    return head.rstrip(), dropped


def human_duration(ms: int) -> str:
    if not ms or ms < 0:
        return ""
    seconds = ms / 1000.0
    if seconds < 1:
        return f"{int(ms)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s".replace(".0s", "s")
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def human_tokens(n: int) -> str:
    if n <= 0:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.2f}M".replace(".00M", "M")


def _dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def fmt_time(ts: str) -> str:
    dt = _dt(ts)
    return dt.strftime("%H:%M:%S") if dt else ""


def fmt_datetime(ts: str) -> str:
    dt = _dt(ts)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def fmt_date(ts: str) -> str:
    dt = _dt(ts)
    return dt.strftime("%Y-%m-%d") if dt else ""


def _escape_summary(text: str) -> str:
    """`<summary>` is HTML, so its content must be HTML-escaped."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


STATUS_MARK = {
    "ok": "",
    "error": " ⚠︎ error",
    "interrupted": " ⚠︎ interrupted",
    "no-result": " ⊘ not run",
    "pending": " … running",
}

LANG_BY_EXT = {
    ".py": "python", ".r": "r", ".R": "r", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".sh": "bash",
    ".zsh": "bash", ".bash": "bash", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".md": "markdown", ".qmd": "markdown", ".rmd": "markdown", ".html": "html",
    ".css": "css", ".scss": "scss", ".sql": "sql", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".java": "java", ".rb": "ruby",
    ".swift": "swift", ".lua": "lua", ".vim": "vim", ".txt": "",
}


def lang_for_path(path: str) -> str:
    return LANG_BY_EXT.get(os.path.splitext(path or "")[1].lower(), "")


# ---------------------------------------------------------------- renderer


class MarkdownRenderer:
    def __init__(self, cfg: dict | None = None, redactor=None):
        md = ((cfg or {}).get("markdown") or {})
        self.tools = md.get("tools", "full")  # full | summary | none
        self.show_thinking = bool(md.get("thinking", True))
        self.max_output = int(md.get("max_output_chars", 4000))
        self.max_input = int(md.get("max_input_chars", 4000))
        self.redact = redactor or (lambda s: s)

    # -- public ---------------------------------------------------------

    def render(self, session: Session) -> str:
        out: list[str] = []
        out.append(self._header(session))
        previous_end = ""
        for rnd in session.rounds:
            gap = self._gap(previous_end, rnd.ts)
            if gap:
                out.append(gap)
            out.append(self._round(rnd, session, level=2))
            previous_end = rnd.end_ts or rnd.ts
        out.append(self._footer(session))
        return "\n".join(part for part in out if part).rstrip() + "\n"

    # -- pieces ---------------------------------------------------------

    def _header(self, session: Session) -> str:
        lines = [f"# {session.title or 'Session'}", ""]

        facts = []
        if session.cwd:
            facts.append(f"`{_tilde(session.cwd)}`")
        if session.git_branch:
            facts.append(f"branch `{session.git_branch}`")
        span = fmt_datetime(session.started)
        if session.updated and fmt_date(session.updated) != fmt_date(session.started):
            span += f" → {fmt_datetime(session.updated)}"
        elif session.updated:
            span += f" → {fmt_time(session.updated)[:5]}"
        if span:
            facts.append(span)
        if facts:
            lines.append(" · ".join(facts))
            lines.append("")

        counts = [
            f"{len(session.rounds)} round{'s' if len(session.rounds) != 1 else ''}",
            f"{session.tool_count} tool call{'s' if session.tool_count != 1 else ''}",
        ]
        if session.usage.total:
            counts.append(f"{human_tokens(session.usage.total)} tokens")
        lines.append("*" + " · ".join(counts) + "*")
        lines.append("")
        return "\n".join(lines)

    def _footer(self, session: Session) -> str:
        bits = [f"session `{session.id[:8]}`"]
        if session.version:
            bits.append(f"Claude Code {session.version}")
        if session.models:
            bits.append(session.models[-1])
        return "\n---\n\n<sub>" + _escape_summary(" · ".join(bits)) + " · logged by scribe</sub>\n"

    def _gap(self, previous_end: str, next_start: str) -> str:
        a, b = _dt(previous_end), _dt(next_start)
        if not a or not b:
            return ""
        minutes = (b - a).total_seconds() / 60.0
        if minutes < GAP_MINUTES:
            return ""
        gap = human_duration(int(minutes * 60_000))
        return f"\n<sub>— {gap} later —</sub>\n"

    def _round(self, rnd: Round, session: Session, level: int = 2) -> str:
        hashes = "#" * level
        who = {"web": "You (web)", "system": "Session", "command": "You"}.get(rnd.source, "You")
        out = [f"\n{hashes} {rnd.index} · {who}", ""]

        meta = [f"`{fmt_time(rnd.ts)}`"]
        if rnd.duration_ms:
            meta.append(human_duration(rnd.duration_ms))
        tools = len(rnd.tool_calls)
        if tools:
            meta.append(f"{tools} tool call{'s' if tools != 1 else ''}")
        if rnd.usage.total:
            meta.append(f"{human_tokens(rnd.usage.total)} tokens")
        out.append("*" + " · ".join(meta) + "*")
        out.append("")

        if rnd.prompt:
            out.append(self.redact(rnd.prompt))
            out.append("")
        if rnd.images:
            out.append(f"*+ {rnd.images} pasted image{'s' if rnd.images != 1 else ''}*")
            out.append("")

        body = self._items(rnd.items, session, level)
        if body:
            out.append(f"{hashes}# Claude")
            out.append("")
            out.append(body)
        return "\n".join(out)

    def _items(self, items: list, session: Session, level: int) -> str:
        chunks: list[str] = []
        for item in items:
            if isinstance(item, Text):
                chunks.append(self.redact(item.md))
            elif isinstance(item, Thinking):
                rendered = self._thinking(item)
                if rendered:
                    chunks.append(rendered)
            elif isinstance(item, ToolCall):
                rendered = self._tool(item, session, level)
                if rendered:
                    chunks.append(rendered)
            elif isinstance(item, Notice):
                chunks.append(self._notice(item))
        return "\n\n".join(c for c in chunks if c)

    def _notice(self, notice: Notice) -> str:
        if notice.variant == "command":
            return f"`{notice.text}`"
        if notice.variant == "compact":
            return f"> ⓘ {notice.text}"
        return f"*{self.redact(notice.text)}*"

    def _thinking(self, item: Thinking) -> str:
        if not self.show_thinking:
            return ""
        label = "Thinking"
        if item.seconds >= 2:
            label = f"Thought for {human_duration(int(item.seconds * 1000))}"
        body = self.redact(item.md)
        return (
            f"<details>\n<summary><i>{_escape_summary(label)}</i></summary>\n\n"
            f"{body}\n\n</details>"
        )

    # -- tool calls -----------------------------------------------------

    def _tool(self, call: ToolCall, session: Session, level: int) -> str:
        if self.tools == "none":
            return ""

        subject = self.redact(call.subject)
        mark = STATUS_MARK.get(call.status, "")
        duration = human_duration(call.duration_ms)
        tail = f" · {duration}" if duration and call.duration_ms > 1500 else ""

        summary = f"<b>{_escape_summary(call.name)}</b>"
        if subject:
            summary += f" — <code>{_escape_summary(subject)}</code>"
        if call.tool_kind == "edit":
            stat = _patch_stat(call.patch)
            if stat:
                summary += f" <code>{stat}</code>"
        summary += _escape_summary(mark + tail)

        if self.tools == "summary":
            return f"- {_strip_tags(summary)}"

        body = self._tool_body(call, session)
        if not body:
            return f"- {_strip_tags(summary)}"
        return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"

    def _tool_body(self, call: ToolCall, session: Session) -> str:
        parts: list[str] = []
        if call.explanation:
            parts.append(f"> {self.redact(call.explanation)}")

        kind = call.tool_kind
        data = call.input if isinstance(call.input, dict) else {}

        if kind == "bash":
            command = self.redact(str(data.get("command") or ""))
            if command:
                parts.append(code_block(command, "bash"))
            parts.append(self._bash_output(call))

        elif kind == "edit":
            diff = _render_patch(call.patch)
            if diff:
                parts.append(code_block(self.redact(diff), "diff"))
            elif call.old_string or call.new_string:
                parts.append(code_block(self.redact(_naive_diff(call.old_string, call.new_string)), "diff"))
            if call.status == "error":
                parts.append(self._result_block(call))

        elif kind == "write":
            content, dropped = clip(str(data.get("content") or ""), self.max_input)
            lang = lang_for_path(str(data.get("file_path") or ""))
            if content:
                parts.append(code_block(self.redact(content), lang))
            if dropped:
                parts.append(f"*… {dropped} more lines*")

        elif kind == "read":
            # The file is still on disk; recording its contents here would
            # multiply the log size for no traceability gain.
            note = _read_note(call)
            if note:
                parts.append(f"*{note}*")
            if call.status == "error":
                parts.append(self._result_block(call))

        elif kind == "task":
            prompt = str(data.get("prompt") or data.get("description") or "")
            snippet, dropped = clip(prompt, 700)
            if snippet:
                parts.append(f"> {self.redact(snippet).replace(chr(10), chr(10) + '> ')}")
            if call.subagent:
                nested = "\n\n".join(
                    self._round(sub, session, level=4) for sub in call.subagent
                )
                parts.append(nested)
            else:
                parts.append(self._result_block(call))

        else:
            args = _pretty_args(data, self.max_input)
            if args:
                parts.append(code_block(self.redact(args), "json"))
            parts.append(self._result_block(call))

        return "\n\n".join(p for p in parts if p)

    def _bash_output(self, call: ToolCall) -> str:
        chunks = []
        stdout, dropped = clip(call.stdout or "", self.max_output)
        if stdout.strip():
            chunks.append(code_block(self.redact(stdout), "text"))
            if dropped:
                chunks.append(f"*… {dropped} more lines*")
        stderr, err_dropped = clip(call.stderr or "", 1500)
        if stderr.strip():
            chunks.append("*stderr*")
            chunks.append(code_block(self.redact(stderr), "text"))
            if err_dropped:
                chunks.append(f"*… {err_dropped} more lines*")
        if not chunks:
            # No sidecar (older transcripts) — fall back to the raw result text.
            if call.result_text.strip():
                text, dropped = clip(call.result_text, self.max_output)
                chunks.append(code_block(self.redact(text), "text"))
                if dropped:
                    chunks.append(f"*… {dropped} more lines*")
            elif call.status == "ok":
                chunks.append("*no output*")
        return "\n\n".join(chunks)

    def _result_block(self, call: ToolCall) -> str:
        text = call.result_text or ""
        if not text.strip():
            if call.result_images:
                return f"*{call.result_images} image{'s' if call.result_images != 1 else ''} returned*"
            return ""
        clipped, dropped = clip(text, self.max_output)
        block = code_block(self.redact(clipped), "text")
        return block + (f"\n\n*… {dropped} more lines*" if dropped else "")


# ---------------------------------------------------------------- helpers


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def _strip_tags(html: str) -> str:
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", html)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text)
    text = re.sub(r"<[^>]+>", "", text)
    return (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    )


def _read_note(call: ToolCall) -> str:
    if call.status == "error":
        return ""
    lines = (call.result_text or "").count("\n")
    if lines:
        return f"read {lines} line{'s' if lines != 1 else ''}"
    if call.result_images:
        return f"read {call.result_images} image{'s' if call.result_images != 1 else ''}"
    return "read"


def _patch_stat(patch: list) -> str:
    added, removed = _patch_counts(patch)
    if not added and not removed:
        return ""
    return f"+{added} −{removed}"


def _patch_counts(patch: list) -> tuple[int, int]:
    added = removed = 0
    for hunk in patch or []:
        for line in (hunk or {}).get("lines", []) if isinstance(hunk, dict) else []:
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return added, removed


def _render_patch(patch: list, max_hunks: int = 12) -> str:
    """`structuredPatch` -> unified diff text."""
    if not patch:
        return ""
    out: list[str] = []
    for hunk in patch[:max_hunks]:
        if not isinstance(hunk, dict):
            continue
        out.append(
            "@@ -%s,%s +%s,%s @@"
            % (
                hunk.get("oldStart", 0),
                hunk.get("oldLines", 0),
                hunk.get("newStart", 0),
                hunk.get("newLines", 0),
            )
        )
        out.extend(str(line) for line in hunk.get("lines", []))
    if len(patch) > max_hunks:
        out.append(f"… {len(patch) - max_hunks} more hunks")
    return "\n".join(out)


def _naive_diff(old: str, new: str, limit: int = 60) -> str:
    """Fallback when no structured patch is available."""
    import difflib

    diff = list(
        difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(), lineterm="", n=2
        )
    )
    diff = [d for d in diff if not d.startswith(("---", "+++"))]
    if len(diff) > limit:
        diff = diff[:limit] + [f"… {len(diff) - limit} more lines"]
    return "\n".join(diff)


def _pretty_args(data: dict, limit: int) -> str:
    import json

    if not data:
        return ""
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    clipped, dropped = clip(text, limit)
    return clipped + (f"\n… {dropped} more lines" if dropped else "")


def render(session: Session, cfg: dict | None = None, redactor=None) -> str:
    return MarkdownRenderer(cfg, redactor).render(session)
