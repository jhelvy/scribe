"""What `/` can complete to: skills and commands, from disk and from Claude.

Two sources, merged:

* **Disk.** ``~/.claude/skills/*/SKILL.md``, ``~/.claude/commands/**/*.md``,
  the same two under the project's ``.claude/``, and every enabled plugin's
  ``skills/`` and ``commands/`` (named ``<plugin>:<name>``). A pure function
  of the filesystem, re-read at most every few seconds.
* **Claude.** A driven session answers ``initialize`` with the full list it
  would offer at its own prompt, descriptions included, and that is the only
  place the bundled skills (``/simplify``, ``/loop``...) and the built-in
  commands come from. The daemon remembers the last such answer in
  ``~/.scribe/cache/commands.json`` so a session with no driver still sees
  them, marked by where they came from.

Each entry::

    {name, description, argument_hint, scope, kind, terminal_only}

``scope`` is ``user`` / ``project`` / ``plugin`` / ``claude``; ``kind`` is
``skill`` (a SKILL.md, invocable by the model too), ``command`` (a prompt
file) or ``builtin`` (Claude Code's own, which only its own prompt
understands). ``terminal_only`` is what Claude Code itself said cannot run
headless.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import paths

#: A sensible minimum when no driver has ever answered `initialize`.
FALLBACK_BUILTINS = [
    ("compact", "Compact the conversation, keeping a summary", "[focus]"),
    ("clear", "Start a fresh context in this session", ""),
    ("model", "Switch model", "<name>"),
    ("effort", "Set the effort level", "<low|medium|high|xhigh|max>"),
    ("context", "Show what is using the context window", ""),
    ("usage", "Show plan usage", ""),
    ("init", "Write a CLAUDE.md for this project", ""),
    ("review", "Review the current changes", ""),
]

_CACHE_TTL_S = 5.0
_cache: dict[str, tuple[float, list[dict]]] = {}


# ---------------------------------------------------------------- frontmatter


def frontmatter(text: str) -> dict:
    """The YAML front matter of a skill or command file, read without YAML.

    Handles what these files actually use: ``key: value``, quoted values,
    and ``>``/``|`` folded blocks. Anything stranger is left as text.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end].strip("\n")
    out: dict = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()
        if value in (">", "|", ">-", "|-"):
            folded = []
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                folded.append(lines[i].strip())
                i += 1
            out[key] = (" " if value.startswith(">") else "\n").join(p for p in folded if p).strip()
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
        i += 1
    return out


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _entry(name: str, meta: dict, scope: str, kind: str) -> dict | None:
    if "user-invocable" in meta and not _truthy(meta["user-invocable"]):
        return None
    return {
        "name": name,
        "description": " ".join(str(meta.get("description") or "").split()),
        "argument_hint": str(meta.get("argument-hint") or meta.get("argumentHint") or "").strip(),
        "scope": scope,
        "kind": kind,
        "terminal_only": False,
    }


# ---------------------------------------------------------------- disk


def _skills_in(folder: Path, scope: str, prefix: str = "") -> list[dict]:
    out = []
    if not folder.is_dir():
        return out
    for child in sorted(folder.iterdir()):
        skill = child / "SKILL.md"
        if not skill.is_file():
            continue
        try:
            meta = frontmatter(skill.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        name = str(meta.get("name") or child.name).strip()
        entry = _entry(prefix + name, meta, scope, "skill")
        if entry:
            out.append(entry)
    return out


def _commands_in(folder: Path, scope: str, prefix: str = "") -> list[dict]:
    out = []
    if not folder.is_dir():
        return out
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        rel = Path(root).relative_to(folder)
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            # Nested directories namespace the command the way Claude Code
            # does: commands/frontend/component.md -> frontend:component.
            parts = [p for p in rel.parts] + [fname[:-3]]
            name = ":".join(parts)
            try:
                meta = frontmatter(Path(root, fname).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            entry = _entry(prefix + name, meta, scope, "command")
            if entry:
                out.append(entry)
    return out


def _enabled_plugins(home: Path) -> list[tuple[str, Path]]:
    """(name, install path) for every plugin that is installed and enabled."""
    installed = paths.read_json(home / "plugins" / "installed_plugins.json", {}) or {}
    settings = paths.read_json(home / "settings.json", {}) or {}
    enabled = settings.get("enabledPlugins") or {}
    out = []
    for key, versions in (installed.get("plugins") or {}).items():
        if enabled and not enabled.get(key, False):
            continue
        name = key.split("@", 1)[0]
        for record in versions if isinstance(versions, list) else []:
            path = Path(str(record.get("installPath") or ""))
            if path.is_dir():
                out.append((name, path))
                break
    return out


def scan(cwd: str = "", home: Path | None = None) -> list[dict]:
    """Everything the filesystem says can be invoked, deduped by name."""
    home = home or paths.claude_home()
    entries: list[dict] = []
    entries += _skills_in(home / "skills", "user")
    entries += _commands_in(home / "commands", "user")
    if cwd:
        entries += _skills_in(Path(cwd) / ".claude" / "skills", "project")
        entries += _commands_in(Path(cwd) / ".claude" / "commands", "project")
    for name, path in _enabled_plugins(home):
        entries += _skills_in(path / "skills", "plugin", name + ":")
        entries += _commands_in(path / "commands", "plugin", name + ":")
    seen: dict[str, dict] = {}
    for entry in entries:
        seen.setdefault(entry["name"], entry)
    return list(seen.values())


def scan_cached(cwd: str = "", home: Path | None = None) -> list[dict]:
    """`scan`, but at most once every few seconds per cwd: the composer asks
    on every keystroke that opens the menu."""
    key = str(cwd or "")
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    found = scan(cwd, home)
    _cache[key] = (now, found)
    return found


# ---------------------------------------------------------------- claude


def cache_file() -> Path:
    return paths.cache_dir() / "commands.json"


def remember(caps: dict) -> None:
    """Keep what a driver reported, for sessions that have none."""
    if not caps or not caps.get("commands"):
        return
    try:
        paths.write_json(
            cache_file(),
            {
                "at": time.time(),
                "commands": caps.get("commands") or [],
                "slash_commands": caps.get("slash_commands") or [],
                "skills": caps.get("skills") or [],
                "terminal_commands": caps.get("terminal_commands") or [],
            },
        )
    except OSError:
        pass


def remembered() -> dict:
    return paths.read_json(cache_file(), {}) or {}


def from_claude(live: dict) -> list[dict]:
    """Entries from a driver's caps (or the remembered copy of one)."""
    skills = set(live.get("skills") or [])
    terminal = set(live.get("terminal_commands") or [])
    items = [dict(i) for i in (live.get("commands") or []) if isinstance(i, dict) and i.get("name")]
    named = {str(i["name"]) for i in items}
    # `initialize` describes the commands; `init` lists them. A name only the
    # list knows (the terminal-only ones, typically) still gets an entry.
    for name in list(live.get("slash_commands") or []) + list(terminal):
        if str(name) not in named:
            items.append({"name": str(name)})
            named.add(str(name))
    out = []
    for item in items:
        name = str(item["name"])
        if name.startswith("__"):
            continue
        out.append(
            {
                "name": name,
                "description": " ".join(str(item.get("description") or "").split()),
                "argument_hint": str(item.get("argument_hint") or item.get("argumentHint") or ""),
                "scope": "claude",
                "kind": "skill" if name in skills else "builtin",
                "terminal_only": name in terminal,
            }
        )
    return out


# ---------------------------------------------------------------- merge


def merge(disk: list[dict], claude: list[dict]) -> list[dict]:
    """Disk entries know their scope; Claude's know the real list. A name in
    both keeps the disk scope and Claude's flags; a name only Claude knows
    is a bundled skill or a built-in."""
    by_name: dict[str, dict] = {}
    for entry in disk:
        by_name[entry["name"]] = dict(entry)
    for entry in claude:
        have = by_name.get(entry["name"])
        if have is not None:
            have["terminal_only"] = entry["terminal_only"]
            if not have["description"]:
                have["description"] = entry["description"]
            if not have["argument_hint"]:
                have["argument_hint"] = entry["argument_hint"]
        else:
            by_name[entry["name"]] = dict(entry)
    if not claude:
        for name, description, hint in FALLBACK_BUILTINS:
            by_name.setdefault(
                name,
                {"name": name, "description": description, "argument_hint": hint, "scope": "claude", "kind": "builtin", "terminal_only": False},
            )
    out = list(by_name.values())
    order = {"project": 0, "user": 1, "plugin": 2, "claude": 3}
    out.sort(key=lambda e: (order.get(e["scope"], 9), e["name"]))
    return out


def catalogue(cwd: str = "", live: dict | None = None, home: Path | None = None) -> tuple[list[dict], str]:
    """The merged list for a session and where Claude's half came from:
    ``live`` (a driver), ``cache`` (a remembered answer) or ``disk`` (none)."""
    disk = scan_cached(cwd, home)
    if live and live.get("commands"):
        return merge(disk, from_claude(live)), "live"
    kept = remembered()
    if kept.get("commands"):
        return merge(disk, from_claude(kept)), "cache"
    return merge(disk, []), "disk"


def available(entry: dict, via: str) -> tuple[bool, str]:
    """Can this entry be sent on this channel, and if not, why not."""
    if entry.get("terminal_only"):
        return False, "only works at Claude Code's own prompt"
    if via in ("driver", "spawn"):
        return True, ""
    if via in ("inbox", "queue"):
        if entry.get("kind") == "builtin":
            return False, "a built-in command: type it in the terminal"
        return True, ""
    return False, "no way to reach this session"


# ---------------------------------------------------------------- files


def list_files(cwd: str, query: str = "", limit: int = 30, cap: int = 20000) -> list[dict]:
    """Paths under ``cwd`` matching ``query``, for `@` mentions without a
    driver. `git ls-files` when it is a repository (it knows what to skip),
    otherwise a bounded walk that skips dot directories and the usual bulk."""
    if not cwd or not os.path.isdir(cwd):
        return []
    files: list[str] = []
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=cwd,
            capture_output=True,
            timeout=5,
        )
        if proc.returncode == 0:
            files = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        files = []
    if not files:
        skip = {"node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build", "target"}
        for root, dirs, names in os.walk(cwd):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in skip)
            rel = os.path.relpath(root, cwd)
            for name in sorted(names):
                files.append(name if rel == "." else os.path.join(rel, name))
                if len(files) >= cap:
                    break
            if len(files) >= cap:
                break
    q = (query or "").lower()
    if q:
        files = [f for f in files if _subsequence(q, f.lower())]
        files.sort(key=lambda f: (q not in os.path.basename(f).lower(), q not in f.lower(), len(f)))
    return [{"path": f} for f in files[:limit]]


def _subsequence(q: str, text: str) -> bool:
    i = 0
    for ch in text:
        if i < len(q) and ch == q[i]:
            i += 1
    return i == len(q)
