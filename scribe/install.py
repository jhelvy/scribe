"""Registering scribe's hooks with Claude Code.

Editing someone's `settings.json` is the most intrusive thing this program does,
so it is careful about it: it backs the file up first, writes through symlinks
rather than replacing them, only ever touches entries it recognises as its own,
and can show you the diff without writing anything.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import paths

MARKER = "scribe-hook"

# event -> (matcher-or-None, timeout seconds)
#
# PermissionRequest gets a long timeout because it is the one event we may
# legitimately hold: when remote approval is armed, the daemon keeps the socket
# open until you decide. Every other event is a fire-and-forget poke that
# returns in single-digit milliseconds.
EVENTS = {
    "SessionStart": (None, 15),
    "SessionEnd": (None, 5),
    "UserPromptSubmit": (None, 10),
    "PostToolUse": ("", 10),
    "PermissionRequest": ("", 300),
    "Notification": (None, 10),
    "Stop": (None, 30),
}


def hook_command() -> str:
    """The command Claude Code runs on every hook event.

    From a clone this is `bin/scribe-hook`, which puts the checkout on
    `sys.path` itself and so works from any directory. In an installed wheel
    that file does not exist and `-m scribe.hookclient` is equivalent — both
    land in the same `main()`. Deciding at install time means the settings
    entry we write is always a command that actually runs.
    """
    entry = Path(__file__).resolve().parent.parent / "bin" / "scribe-hook"
    if entry.is_file():
        return f'"{sys.executable}" "{entry}"'
    return f'"{sys.executable}" -m scribe.hookclient'


def settings_path(project: bool = False) -> Path:
    if project:
        return Path(os.getcwd()) / ".claude" / "settings.json"
    return paths.claude_home() / "settings.json"


def _read(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    """Write settings, preserving a symlink rather than replacing it.

    A dotfiles setup usually has ~/.claude/settings.json symlinked into a repo.
    Writing a temp file and renaming over the link would silently detach it, so
    the link is resolved first and the real file written in place.
    """
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    if target.is_symlink() or target.exists():
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        paths.write_atomic(target, text)


def _backup(path: Path) -> Path | None:
    real = path.resolve() if path.is_symlink() else path
    if not real.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = real.with_name(f"{real.name}.scribe-backup-{stamp}")
    try:
        shutil.copy2(real, backup)
        return backup
    except OSError:
        return None


def _is_ours(entry: dict) -> bool:
    return MARKER in str((entry or {}).get("command") or "")


def _clean(settings: dict) -> dict:
    """Remove our entries, and any group left empty by their removal."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        surviving_groups = []
        for group in groups:
            if not isinstance(group, dict):
                surviving_groups.append(group)
                continue
            inner = [h for h in (group.get("hooks") or []) if not _is_ours(h)]
            if inner:
                surviving_groups.append({**group, "hooks": inner})
            elif not group.get("hooks"):
                surviving_groups.append(group)
        if surviving_groups:
            hooks[event] = surviving_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return settings


def _apply(settings: dict) -> dict:
    settings = _clean(settings)
    hooks = settings.setdefault("hooks", {})
    command = hook_command()
    for event, (matcher, timeout) in EVENTS.items():
        entry = {"type": "command", "command": command, "timeout": timeout}
        group: dict = {"hooks": [entry]}
        if matcher is not None:
            group["matcher"] = matcher
        existing = hooks.get(event)
        if isinstance(existing, list):
            existing.append(group)
        else:
            hooks[event] = [group]
    return settings


def is_installed(project: bool = False) -> bool:
    settings = _read(settings_path(project))
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        for group in groups if isinstance(groups, list) else []:
            for entry in (group or {}).get("hooks", []) if isinstance(group, dict) else []:
                if _is_ours(entry):
                    return True
    return False


def install(project: bool = False, dry_run: bool = False) -> int:
    path = settings_path(project)
    current = _read(path)
    updated = _apply(json.loads(json.dumps(current)))

    if dry_run:
        print(f"would write {path}" + (f" (via symlink -> {path.resolve()})" if path.is_symlink() else ""))
        print(json.dumps(updated.get("hooks", {}), indent=2))
        return 0

    backup = _backup(path)
    _write(path, updated)

    print(f"hooks installed in {path}")
    if path.is_symlink():
        print(f"  (symlink, wrote through to {path.resolve()})")
    if backup:
        print(f"  backup: {backup}")
    print(f"  events: {', '.join(sorted(EVENTS))}")
    print("\nStart a new Claude Code session to pick them up.")
    if not paths.control_socket().exists():
        print("The daemon starts itself on SessionStart; `scribe serve` runs it now.")
    return 0


def uninstall(project: bool = False, dry_run: bool = False) -> int:
    path = settings_path(project)
    current = _read(path)
    if not is_installed(project):
        print(f"no scribe hooks in {path}")
        return 0
    updated = _clean(json.loads(json.dumps(current)))
    if dry_run:
        print(f"would write {path}")
        print(json.dumps(updated.get("hooks", {}), indent=2))
        return 0
    backup = _backup(path)
    _write(path, updated)
    print(f"hooks removed from {path}")
    if backup:
        print(f"  backup: {backup}")
    return 0
