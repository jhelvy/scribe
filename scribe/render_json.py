"""Session -> the JSON the viewer consumes.

Sibling of :mod:`scribe.render_md`, fed by the same model. Where the markdown
edits for readability, this keeps the detail the UI can afford to show on
demand: full commands, full stdout and stderr, structured diff hunks, per-call
timings and token counts.

Bounded on purpose. A single `Read` of a large file can be megabytes, and
shipping that down an SSE stream would stall the browser for no benefit — so
payloads are clipped here with an explicit `truncated` flag, and the viewer can
ask for the whole thing through ``/api/tool`` if the reader actually wants it.
"""

from __future__ import annotations

from .model import Notice, Round, Session, Text, Thinking, ToolCall
from .render_md import _patch_counts, human_duration, human_tokens, lang_for_path

MAX_TEXT = 200_000
MAX_OUTPUT = 60_000
MAX_INPUT = 40_000


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if not text:
        return "", False
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit], True


class JsonRenderer:
    def __init__(self, redactor=None):
        self.redact = redactor or (lambda s: s)
        self.redact_data = getattr(redactor, "scrub_data", None) or (lambda d: d)

    def session(self, session: Session, include_rounds: bool = True) -> dict:
        out = {
            "id": session.id,
            "cwd": session.cwd,
            "project": session.project,
            "title": session.title,
            "started": session.started,
            "updated": session.updated,
            "version": session.version,
            "git_branch": session.git_branch,
            "models": session.models,
            "usage": session.usage.as_dict(),
            "usage_label": human_tokens(session.usage.total),
            "transcript_path": session.transcript_path,
            "log_path": session.log_path,
            "round_count": len(session.rounds),
            "tool_count": session.tool_count,
        }
        if include_rounds:
            out["rounds"] = [self.round(r) for r in session.rounds]
        return out

    def round(self, rnd: Round) -> dict:
        return {
            "index": rnd.index,
            "uuid": rnd.uuid,
            "ts": rnd.ts,
            "end_ts": rnd.end_ts,
            "prompt": self.redact(rnd.prompt),
            "source": rnd.source,
            "images": rnd.images,
            "duration_ms": rnd.duration_ms,
            "duration_label": human_duration(rnd.duration_ms),
            "usage": rnd.usage.as_dict(),
            "usage_label": human_tokens(rnd.usage.total),
            "tool_count": len(rnd.tool_calls),
            "items": [self.item(i) for i in rnd.items],
        }

    def item(self, item) -> dict:
        if isinstance(item, Text):
            md, truncated = _clip(self.redact(item.md), MAX_TEXT)
            return {"kind": "text", "uuid": item.uuid, "ts": item.ts, "md": md, "truncated": truncated}
        if isinstance(item, Thinking):
            md, truncated = _clip(self.redact(item.md), MAX_TEXT)
            return {
                "kind": "thinking",
                "uuid": item.uuid,
                "ts": item.ts,
                "md": md,
                "truncated": truncated,
                "seconds": round(item.seconds, 1),
                "label": human_duration(int(item.seconds * 1000)),
            }
        if isinstance(item, Notice):
            return {
                "kind": "notice",
                "uuid": item.uuid,
                "ts": item.ts,
                "text": self.redact(item.text),
                "variant": item.variant,
            }
        if isinstance(item, ToolCall):
            return self.tool(item)
        return {"kind": "unknown", "ts": getattr(item, "ts", "")}

    def tool(self, call: ToolCall) -> dict:
        data = call.input if isinstance(call.input, dict) else {}
        added, removed = _patch_counts(call.patch)
        command, command_truncated = _clip(self.redact(str(data.get("command") or "")), MAX_INPUT)
        content, content_truncated = _clip(self.redact(str(data.get("content") or "")), MAX_INPUT)
        stdout, stdout_truncated = _clip(self.redact(call.stdout), MAX_OUTPUT)
        stderr, _ = _clip(self.redact(call.stderr), 20_000)
        result, result_truncated = _clip(self.redact(call.result_text), MAX_OUTPUT)

        out = {
            "kind": "tool",
            "uuid": call.uuid,
            "ts": call.ts,
            "id": call.id,
            "name": call.name,
            "tool_kind": call.tool_kind,
            "subject": self.redact(call.subject),
            "status": call.status,
            "needs_approval": call.needs_approval,
            "duration_ms": call.duration_ms,
            "duration_label": human_duration(call.duration_ms),
            "explanation": call.explanation,
            "explanation_tier": call.explanation_tier,
            "agent_name": call.agent_name,
            "file_path": call.file_path or str(data.get("file_path") or ""),
            "lang": lang_for_path(call.file_path or str(data.get("file_path") or "")),
            "command": command,
            "content": content,
            "stdout": stdout,
            "stderr": stderr,
            "result_text": result,
            "result_images": call.result_images,
            "truncated": command_truncated or content_truncated or stdout_truncated or result_truncated,
            "patch": call.patch,
            "added": added,
            "removed": removed,
            "input": self.redact_data(_bounded_input(data)),
        }
        if call.subagent:
            out["subagent"] = [self.round(r) for r in call.subagent]
        return out


def _bounded_input(data: dict) -> dict:
    """Drop the fields already surfaced explicitly, cap what is left.

    Without this the viewer receives every Write's whole file body twice: once as
    ``content`` and once inside the raw argument dump.
    """
    skip = {"command", "content", "old_string", "new_string", "originalFile"}
    out = {}
    for key, value in (data or {}).items():
        if key in skip:
            continue
        if isinstance(value, str) and len(value) > 8000:
            out[key] = value[:8000] + "…"
        else:
            out[key] = value
    return out


def render(session: Session, redactor=None, include_rounds: bool = True) -> dict:
    return JsonRenderer(redactor).session(session, include_rounds)
