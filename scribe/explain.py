"""Plain-English explanations of opaque tool calls.

A `python3 - <<'EOF'` heredoc or a piped shell chain is not something a person
can evaluate at a glance, and the terminal shows it raw. This turns it into one
or two sentences, in the margin, next to the call — ideally while you are still
deciding whether to approve it.

Three rules keep it cheap and safe:

* **Cheap calls never cost anything.** A `Read` explains itself from its own
  arguments; only genuinely opaque calls reach a model.
* **Answers are cached by content.** Approving `git status` once explains it
  forever, across sessions and projects.
* **Nothing ever blocks.** The model runs in a thread, out of the hook's path,
  and its result is pushed to the browser whenever it lands.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths

SYSTEM_PROMPT = (
    "You explain one tool call that an AI coding agent is asking permission to "
    "run. Your reader is the developer deciding whether to approve it, and they "
    "cannot read the raw command. Reply with one or two plain sentences and "
    "nothing else — no preamble, no markdown, no code. Stay under 55 words. Say "
    "concretely what the call does, and name any consequence that is not "
    "obvious: files created, overwritten, edited in place, or deleted; network "
    "requests; packages installed; anything hard to undo. If it only reads or "
    "inspects, say so plainly so the reader can relax. Describe only what the "
    "call itself shows. Do not guess at the agent's wider intent, and do not "
    "judge whether approving is a good idea."
)

# Shapes that are opaque regardless of length: the argument text alone does not
# tell you what will happen.
OPAQUE_MARKERS = (
    "<<", "-c '", '-c "', "-e '", '-e "', "eval ", "base64", "| sh", "|sh",
    "| bash", "|bash", "rm -rf", "sudo ", "curl ", "wget ", "chmod ", "> /",
    "dd ", "mkfs", ":(){", "xargs ",
)

DISALLOWED_TOOLS = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "Agent", "NotebookEdit", "Skill",
]

# If the CLI answers with one of these, it is reporting its own failure rather
# than explaining anything. Rendering it as an explanation would be worse than
# rendering nothing.
ERROR_HINTS = (
    "please run /login", "not logged in", "invalid api key", "credit balance is too low",
    "usage limit reached", "authentication_error", "command not found",
)

CANNED = {
    "Read": "Reads {subject} without changing it.",
    "NotebookRead": "Reads the notebook {subject} without changing it.",
    "Glob": "Lists files matching {subject}. Read-only.",
    "Grep": "Searches file contents for {subject}. Read-only.",
    "TodoWrite": "Updates the agent's own task list. Touches nothing on disk.",
    "TaskCreate": "Adds a task to the agent's task list.",
    "TaskUpdate": "Updates a task in the agent's task list.",
    "TaskList": "Reads the agent's task list.",
    "TaskGet": "Reads one task from the agent's task list.",
    "WebSearch": "Runs a web search for {subject}.",
    "WebFetch": "Fetches {subject} and reads the page. Sends a request to that site.",
    "Write": "Writes {subject}, replacing anything already there.",
    "Edit": "Edits {subject} in place.",
    "NotebookEdit": "Edits the notebook {subject} in place.",
    "Agent": "Hands work to a subagent: {subject}.",
    "Task": "Hands work to a subagent: {subject}.",
    "Skill": "Loads the {subject} skill's instructions.",
    "AskUserQuestion": "Asks you a question and waits for your answer.",
    "ExitPlanMode": "Presents a plan for your approval and leaves plan mode.",
}


class Explainer:
    def __init__(self, cfg: dict, on_ready=None):
        self.cfg = cfg
        self.on_ready = on_ready or (lambda call_id, text, tier: None)
        self._cache: dict[str, str] = {}
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._dirty = False
        self._load_cache()

    # -- settings -------------------------------------------------------

    @property
    def settings(self) -> dict:
        return self.cfg.get("explain") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True)) and self.settings.get("scope") != "off"

    # -- classification -------------------------------------------------

    def canned(self, tool_name: str, tool_input: dict) -> str:
        """A free one-liner built from the call's own arguments, or ""."""
        from .build import tool_subject

        template = CANNED.get(tool_name)
        if not template:
            if tool_name.startswith("mcp__"):
                return f"Calls the {tool_name} MCP tool."
            return ""
        subject = tool_subject(tool_name, tool_input or {})
        if "{subject}" in template and not subject:
            return ""
        return template.format(subject=f"`{subject}`" if subject else "")

    def needs_model(self, tool_name: str, tool_input: dict) -> bool:
        if not self.enabled:
            return False
        if tool_name in (self.settings.get("canned_tools") or []):
            return False
        subject = self._subject_text(tool_name, tool_input)
        if any(marker in subject for marker in OPAQUE_MARKERS):
            return True
        return len(subject) >= int(self.settings.get("min_chars", 60))

    def _subject_text(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "Bash":
            return str((tool_input or {}).get("command") or "")
        try:
            return json.dumps(tool_input or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(tool_input)

    # -- cache ----------------------------------------------------------

    @staticmethod
    def key_for(tool_name: str, tool_input: dict) -> str:
        try:
            blob = json.dumps(tool_input or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = str(tool_input)
        return hashlib.sha256(f"{tool_name}\x00{blob}".encode("utf-8")).hexdigest()[:32]

    def _load_cache(self) -> None:
        data = paths.read_json(paths.explanations_cache(), {}) or {}
        if isinstance(data, dict):
            self._cache = {k: v for k, v in data.items() if isinstance(v, str)}

    def _save_cache(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(self._cache)
            self._dirty = False
        # Bound the file: oldest-inserted go first, which in a dict is insertion
        # order, and is a good enough proxy for least-recently-useful here.
        if len(snapshot) > 4000:
            snapshot = dict(list(snapshot.items())[-3000:])
        paths.ensure_dirs()
        paths.write_json(paths.explanations_cache(), snapshot)

    # -- the request ----------------------------------------------------

    def lookup(self, tool_name: str, tool_input: dict) -> str:
        """Cache-only read. Never spawns anything, never costs anything.

        Called for every tool call on every rebuild, which is what makes an
        explanation stick: once a command has been explained, it is explained
        everywhere it appears — in this session, in older logs, and in the
        markdown — without asking a model again.
        """
        with self._lock:
            return self._cache.get(self.key_for(tool_name, tool_input), "")

    def request(self, call_id: str, tool_name: str, tool_input: dict, force: bool = False) -> None:
        if not force and not self.needs_model(tool_name, tool_input):
            return
        key = self.key_for(tool_name, tool_input)
        with self._lock:
            cached = self._cache.get(key)
            if cached:
                self.on_ready(call_id, cached, 1)
                return
            if key in self._inflight:
                return
            self._inflight.add(key)
        threading.Thread(
            target=self._work,
            args=(call_id, key, tool_name, tool_input),
            daemon=True,
            name="scribe-explain",
        ).start()

    def _work(self, call_id: str, key: str, tool_name: str, tool_input: dict) -> None:
        try:
            text = self._ask_model(tool_name, tool_input)
        except Exception:
            text = ""
        finally:
            with self._lock:
                self._inflight.discard(key)
        if not text:
            return
        with self._lock:
            self._cache[key] = text
            self._dirty = True
        self._save_cache()
        self.on_ready(call_id, text, 1)

    def build_argv(self, claude: str, tool_name: str, tool_input: dict) -> list[str]:
        """The exact command line the explainer child runs.

        Separated from :meth:`_ask_model` so the isolation flags can be asserted
        in a test without spawning anything: getting these wrong is how the
        plugin ends up recursively logging its own explainer.
        """
        message = _user_message(tool_name, tool_input)
        return [
            claude, "-p",
            "--model", str(self.settings.get("model") or "claude-haiku-4-5"),
            # No user settings, no MCP servers, no tools: this must be a single
            # cheap completion and nothing else. It also stops our own hooks
            # from firing inside the child.
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disallowed-tools", *DISALLOWED_TOOLS,
            "--system-prompt", SYSTEM_PROMPT,
            message,
        ]

    def _ask_model(self, tool_name: str, tool_input: dict) -> str:
        claude = _find_claude()
        if not claude:
            return ""
        argv = self.build_argv(claude, tool_name, tool_input)

        env = dict(os.environ)
        env["SCRIBE_DISABLE"] = "1"

        scratch = workdir()
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=float(self.settings.get("timeout_s", 25)),
                env=env,
                cwd=str(scratch),
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        if proc.returncode != 0:
            return ""
        return clean(proc.stdout)


def workdir() -> Path:
    """Where explainer children run.

    A scratch directory inside our own root, for two reasons: the indexer
    recognises the path and drops the child's transcripts from the session list,
    and :func:`prune_transcripts` knows exactly which directory to sweep.
    """
    path = paths.run_dir() / "explain"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prune_transcripts(max_age_s: float = 3600) -> int:
    """Delete transcripts left behind by explainer children.

    Every explanation runs a real `claude -p`, and Claude Code writes a
    transcript for it. They are invisible in the viewer but would otherwise
    accumulate in ~/.claude/projects forever — a few kilobytes per explained
    command, on someone else's disk, for no benefit.
    """
    import time as _time

    mangled = str(workdir()).replace("/", "-")
    folder = paths.projects_dir() / mangled
    if not folder.is_dir():
        return 0
    removed = 0
    cutoff = _time.time() - max_age_s
    try:
        for child in folder.glob("*.jsonl"):
            try:
                if child.stat().st_mtime < cutoff:
                    child.unlink()
                    removed += 1
            except OSError:
                continue
        if not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
    return removed


def attach(session, explainer: "Explainer | None" = None, cfg: dict | None = None) -> None:
    """Fill in every tool call in a session that has a cached explanation.

    Cache-only and free. Called from :mod:`scribe.store`, so an explanation
    written once reaches the markdown, the viewer, and an exported HTML file
    alike — including for sessions recorded long before it was asked for.
    """
    if explainer is None:
        from . import config as _config

        explainer = Explainer(cfg or _config.load())

    def walk(rounds):
        for rnd in rounds:
            for call in rnd.tool_calls:
                if not call.explanation:
                    text = explainer.lookup(call.name, call.input)
                    if text:
                        call.explanation, call.explanation_tier = text, 1
                if call.subagent:
                    walk(call.subagent)

    walk(session.rounds)


def find_call(session, call_id: str):
    """Locate a tool call anywhere in a session, including inside subagents."""

    def walk(rounds):
        for rnd in rounds:
            for call in rnd.tool_calls:
                if call.id == call_id:
                    return call
                if call.subagent:
                    found = walk(call.subagent)
                    if found is not None:
                        return found
        return None

    return walk(session.rounds)


def _user_message(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        command = str((tool_input or {}).get("command") or "")
        return f"Tool: Bash\n\nCommand:\n{_truncate(command, 4000)}"
    try:
        blob = json.dumps(tool_input or {}, indent=2, default=str)
    except (TypeError, ValueError):
        blob = str(tool_input)
    return f"Tool: {tool_name}\n\nArguments:\n{_truncate(blob, 4000)}"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def clean(text: str) -> str:
    """Normalise the model's reply, or return "" if it is not an explanation."""
    out = (text or "").strip()
    if not out:
        return ""
    lowered = out.lower()
    if any(hint in lowered for hint in ERROR_HINTS):
        return ""
    if out.startswith("```"):
        lines = [l for l in out.splitlines() if not l.strip().startswith("```")]
        out = "\n".join(lines).strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'":
        out = out[1:-1].strip()
    out = " ".join(out.split())
    return out[:600]


_CLAUDE_PATH: str | None = None


def _find_claude() -> str | None:
    global _CLAUDE_PATH
    if _CLAUDE_PATH is not None:
        return _CLAUDE_PATH or None
    import shutil

    found = shutil.which("claude") or ""
    if not found:
        for candidate in (
            Path.home() / ".claude" / "local" / "claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"),
        ):
            if candidate.exists():
                found = str(candidate)
                break
    _CLAUDE_PATH = found
    return found or None
