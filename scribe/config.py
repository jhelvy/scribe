"""Configuration: defaults, ``~/.scribe/config.json``, environment overrides.

Loading never raises. A malformed file or one bad key falls back to the default
for that key rather than taking down a hook or the daemon.
"""

from __future__ import annotations

import copy
import os
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    # Web server
    "port": 4517,
    "open_browser": True,
    # Markdown rendering
    "markdown": {
        "enabled": True,
        "tools": "full",  # full | summary | none
        "thinking": True,
        "max_output_chars": 4000,
        "max_input_chars": 4000,
    },
    # Margin explanations of opaque tool calls
    "explain": {
        "enabled": True,
        "model": "claude-haiku-4-5",
        "scope": "permission",  # off | permission | all
        "min_chars": 60,
        "timeout_s": 25,
        "canned_tools": [
            "Read",
            "Glob",
            "Grep",
            "NotebookRead",
            "TodoWrite",
            "TaskCreate",
            "TaskUpdate",
            "TaskList",
            "TaskGet",
        ],
    },
    # Two-way control. Both default off: a held hook is a stalled terminal, and
    # an injected reply is a real message. Opting in should be a deliberate act.
    "remote_approval": {
        "enabled": False,
        "wait_s": 120,
        "require_client": True,  # never hold if no browser is listening
    },
    "reply_queue": {
        "enabled": False,
        "max_chain": 5,  # consecutive Stop-hook injections before we stand down
    },
    # Messages typed on the page. Delivered through Claude Code's own session
    # inbox, so they land exactly as a prompt typed in the terminal would. On
    # by default: every message is an explicit act, and Claude Code applies its
    # own inbound policy (a bypass-mode session asks before accepting one).
    "messaging": {
        "enabled": True,
    },
    # A session with no process behind it gets a headless Claude Code child
    # of our own (`claude -p --input-format stream-json --resume <id>`), kept
    # between turns and closed after `idle_min` of silence. The page can pick
    # its permission mode and model; `bypassPermissions` only if allowed here.
    "driver": {
        "enabled": True,
        "idle_min": 30,
        "default_mode": "",  # "" = Claude Code's own permissions.defaultMode
        "default_model": "",  # "" = the account default
        "allow_bypass": False,
    },
    # Secret scrubbing applied to markdown and the viewer payload alike
    "redact": {
        "enabled": True,
        "extra_patterns": [],
    },
    "watch_interval_ms": 300,
    # Sessions idle longer than this are not polled (they can still be opened).
    "active_window_min": 180,
}

def _bool(value: str) -> bool:
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


# Flat dotted path -> (environment variable, caster).
ENV_OVERRIDES = {
    "port": ("SCRIBE_PORT", int),
    "explain.enabled": ("SCRIBE_EXPLAIN", _bool),
    "explain.model": ("SCRIBE_EXPLAIN_MODEL", str),
    "explain.scope": ("SCRIBE_EXPLAIN_SCOPE", str),
    "explain.min_chars": ("SCRIBE_EXPLAIN_MIN_CHARS", int),
    "explain.timeout_s": ("SCRIBE_EXPLAIN_TIMEOUT", int),
    "remote_approval.enabled": ("SCRIBE_REMOTE_APPROVAL", _bool),
    "remote_approval.wait_s": ("SCRIBE_APPROVAL_WAIT", int),
    "reply_queue.enabled": ("SCRIBE_REPLY_QUEUE", _bool),
    "messaging.enabled": ("SCRIBE_MESSAGING", _bool),
    "driver.enabled": ("SCRIBE_DRIVER", _bool),
    "driver.idle_min": ("SCRIBE_DRIVER_IDLE_MIN", int),
    "driver.default_mode": ("SCRIBE_DRIVER_MODE", str),
    "driver.default_model": ("SCRIBE_DRIVER_MODEL", str),
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        elif key in out:
            # Keep the default when the type is obviously wrong; a string where
            # an int belongs should not propagate into arithmetic later.
            if out[key] is None or isinstance(value, type(out[key])) or isinstance(out[key], type(value)):
                out[key] = value
        else:
            out[key] = value
    return out


def load() -> dict:
    cfg = _deep_merge(DEFAULTS, paths.read_json(paths.config_file(), {}) or {})
    for dotted, (env_name, caster) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            set_in(cfg, dotted, caster(raw))
        except (TypeError, ValueError):
            pass
    return cfg


def save(cfg: dict) -> None:
    paths.ensure_dirs()
    paths.write_json(paths.config_file(), cfg)


def get_in(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_in(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def coerce(dotted: str, raw: str):
    """Cast a CLI string against the default's type so ``config set`` is typed."""
    current = get_in(DEFAULTS, dotted, None)
    if isinstance(current, bool):
        return str(raw).lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def flatten(cfg: dict, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in sorted(cfg.items()):
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(flatten(value, dotted + "."))
        else:
            out.append((dotted, value))
    return out


def known_key(dotted: str) -> bool:
    sentinel = object()
    return get_in(DEFAULTS, dotted, sentinel) is not sentinel
