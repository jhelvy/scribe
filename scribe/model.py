"""The session model.

A session is a list of **rounds**. A round opens at a real user prompt and
closes at the next one; everything Claude does in between — text, thinking, tool
calls — is an ordered list of items inside it. That is how a person reads a
transcript, so it is the unit the markdown renderer paragraphs on and the unit
the viewer keys its DOM on.

The model is a pure function of the transcript (see :mod:`scribe.build`), which
is what lets the markdown be regenerated at any time instead of appended to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Tool families. The name drives how a call is rendered, and grouping the long
# tail into "other" keeps the viewer from needing a branch per tool.
KIND_BY_TOOL = {
    "Bash": "bash",
    "BashOutput": "bash",
    "KillShell": "bash",
    "Edit": "edit",
    "NotebookEdit": "edit",
    "MultiEdit": "edit",
    "Write": "write",
    "Read": "read",
    "NotebookRead": "read",
    "Glob": "search",
    "Grep": "search",
    "ToolSearch": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Agent": "task",
    "Task": "task",
    "Skill": "task",
    "TodoWrite": "todo",
    "TaskCreate": "todo",
    "TaskUpdate": "todo",
    "TaskList": "todo",
    "TaskGet": "todo",
    "AskUserQuestion": "ask",
    "ExitPlanMode": "plan",
    "EnterPlanMode": "plan",
}


def tool_kind(name: str) -> str:
    if name in KIND_BY_TOOL:
        return KIND_BY_TOOL[name]
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        """Tokens genuinely produced or consumed fresh.

        Deliberately excludes ``cache_read``. Every assistant message re-reads
        the whole cached prefix, so summing that across a long session produces
        a number like "84M tokens" for a conversation that generated a few
        hundred thousand — technically the sum of a real field, and useless.
        Cache volume is still reported, just not as the headline.
        """
        return self.input_tokens + self.output_tokens + self.cache_write

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write

    @classmethod
    def from_raw(cls, raw: Any) -> "Usage":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            cache_read=int(raw.get("cache_read_input_tokens") or 0),
            cache_write=int(raw.get("cache_creation_input_tokens") or 0),
        )

    def as_dict(self) -> dict:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "total": self.total,
        }

    def __bool__(self) -> bool:
        return bool(self.total or self.cache_read)


@dataclass
class Item:
    """Base for everything that can appear inside a round."""

    kind: str = "item"
    uuid: str = ""
    ts: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "uuid": self.uuid, "ts": self.ts}


@dataclass
class Text(Item):
    md: str = ""

    def __post_init__(self) -> None:
        self.kind = "text"

    def as_dict(self) -> dict:
        return {**super().as_dict(), "md": self.md}


@dataclass
class Thinking(Item):
    md: str = ""
    seconds: float = 0.0

    def __post_init__(self) -> None:
        self.kind = "thinking"

    def as_dict(self) -> dict:
        return {**super().as_dict(), "md": self.md, "seconds": round(self.seconds, 1)}


@dataclass
class Notice(Item):
    """A small out-of-band event: a slash command, a compact boundary, a resume."""

    text: str = ""
    variant: str = "info"  # info | command | compact | error | web

    def __post_init__(self) -> None:
        self.kind = "notice"

    def as_dict(self) -> dict:
        return {**super().as_dict(), "text": self.text, "variant": self.variant}


@dataclass
class ToolCall(Item):
    id: str = ""
    name: str = ""
    tool_kind: str = "other"
    input: dict = field(default_factory=dict)
    subject: str = ""  # the one-line "what is this call" summary
    status: str = "pending"  # pending | ok | error | interrupted | no-result
    result_text: str = ""
    stdout: str = ""
    stderr: str = ""
    patch: list = field(default_factory=list)  # structuredPatch hunks
    file_path: str = ""
    old_string: str = ""
    new_string: str = ""
    result_images: int = 0
    duration_ms: int = 0
    explanation: str = ""
    explanation_tier: int = 0  # 0 canned, 1 model-written
    needs_approval: bool = False
    subagent: list = field(default_factory=list)  # nested Rounds for Task calls
    agent_name: str = ""

    def __post_init__(self) -> None:
        self.kind = "tool"

    def as_dict(self) -> dict:
        out = {
            **super().as_dict(),
            "id": self.id,
            "name": self.name,
            "tool_kind": self.tool_kind,
            "input": self.input,
            "subject": self.subject,
            "status": self.status,
            "result_text": self.result_text,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "patch": self.patch,
            "file_path": self.file_path,
            "result_images": self.result_images,
            "duration_ms": self.duration_ms,
            "explanation": self.explanation,
            "explanation_tier": self.explanation_tier,
            "needs_approval": self.needs_approval,
            "agent_name": self.agent_name,
        }
        if self.subagent:
            out["subagent"] = [r.as_dict() for r in self.subagent]
        return out


@dataclass
class Round:
    index: int = 0
    uuid: str = ""
    ts: str = ""
    end_ts: str = ""
    prompt: str = ""
    source: str = "user"  # user | web | peer | command | system
    items: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    usage_by_model: dict = field(default_factory=dict)  # model -> Usage.total
    duration_ms: int = 0
    images: int = 0
    # What came with the prompt: pictures pasted or attached (as content
    # blocks, addressed by row uuid + block index) and files named by path.
    attachments: list = field(default_factory=list)

    @property
    def tool_calls(self) -> list:
        return [i for i in self.items if isinstance(i, ToolCall)]

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "uuid": self.uuid,
            "ts": self.ts,
            "end_ts": self.end_ts,
            "prompt": self.prompt,
            "source": self.source,
            "items": [i.as_dict() for i in self.items],
            "usage": self.usage.as_dict(),
            "duration_ms": self.duration_ms,
            "images": self.images,
            "attachments": list(self.attachments),
            "tool_count": len(self.tool_calls),
        }


@dataclass
class Session:
    id: str = ""
    cwd: str = ""
    project: str = ""
    title: str = ""
    slug: str = ""
    started: str = ""
    updated: str = ""
    version: str = ""
    git_branch: str = ""
    models: list = field(default_factory=list)
    rounds: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    usage_by_model: dict = field(default_factory=dict)  # model -> Usage.total
    transcript_path: str = ""
    log_path: str = ""

    @property
    def tool_count(self) -> int:
        return sum(len(r.tool_calls) for r in self.rounds)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "project": self.project,
            "title": self.title,
            "slug": self.slug,
            "started": self.started,
            "updated": self.updated,
            "version": self.version,
            "git_branch": self.git_branch,
            "models": self.models,
            "usage": self.usage.as_dict(),
            "transcript_path": self.transcript_path,
            "log_path": self.log_path,
            "round_count": len(self.rounds),
            "tool_count": self.tool_count,
            "rounds": [r.as_dict() for r in self.rounds],
        }

    def head_dict(self) -> dict:
        """Everything except the rounds — for the session switcher."""
        out = self.as_dict()
        out.pop("rounds", None)
        return out
