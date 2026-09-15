"""Filesystem layout for scribe.

Everything scribe owns lives under ``~/.scribe`` (mode 0700), deliberately
outside every repository: the logs now contain full tool output, so keeping them
off a working tree is the difference between a private archive and an accidental
``git add -A``.

::

    ~/.scribe/
      config.json
      logs/<project-slug>/<YYYY-MM-DD>-<title-slug>.md
      state/<session-id>.json      per-session sidecar (web messages, arm state)
      state/projects.json          cwd -> project-slug registry
      cache/explanations.json
      uploads/<session-id>/<id>-<name>   files attached from the page
      run/control.sock  run/server.json  run/daemon.pid

The Claude Code side is read-only to us: transcripts live in
``~/.claude/projects/<mangled-cwd>/<session-id>.jsonl``.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

APP = "scribe"

# ---------------------------------------------------------------- roots


def home() -> Path:
    return Path(os.path.expanduser("~"))


def root() -> Path:
    """The scribe data directory. ``SCRIBE_HOME`` overrides (tests use it)."""
    env = os.environ.get("SCRIBE_HOME")
    return Path(env).expanduser() if env else home() / ".scribe"


def claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else home() / ".claude"


def projects_dir() -> Path:
    """Where Claude Code keeps its JSONL transcripts."""
    return claude_home() / "projects"


def config_file() -> Path:
    return root() / "config.json"


def logs_dir() -> Path:
    return root() / "logs"


def state_dir() -> Path:
    return root() / "state"


def cache_dir() -> Path:
    return root() / "cache"


def run_dir() -> Path:
    return root() / "run"


def uploads_dir(session_id: str = "") -> Path:
    base = root() / "uploads"
    return base / safe_component(session_id) if session_id else base


def control_socket() -> Path:
    from .sockpath import control_socket_path

    return Path(control_socket_path())


def server_record() -> Path:
    return run_dir() / "server.json"


def pid_file() -> Path:
    return run_dir() / "daemon.pid"


def explanations_cache() -> Path:
    return cache_dir() / "explanations.json"


def session_state(session_id: str) -> Path:
    return state_dir() / f"{safe_component(session_id)}.json"


def ensure_dirs() -> None:
    """Create the whole tree with private permissions. Idempotent."""
    r = root()
    r.mkdir(parents=True, exist_ok=True)
    _chmod700(r)
    for d in (logs_dir(), state_dir(), cache_dir(), run_dir(), uploads_dir()):
        d.mkdir(parents=True, exist_ok=True)
        _chmod700(d)


def _chmod700(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass  # Windows, exotic filesystems: not worth failing over


# ---------------------------------------------------------------- slugs

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(text: str, max_len: int = 60) -> str:
    """Lowercase ASCII slug. Empty input yields ``untitled``."""
    if not text:
        return "untitled"
    norm = unicodedata.normalize("NFKD", text)
    norm = norm.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", norm).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "untitled"


def safe_component(text: str) -> str:
    """A single path component that cannot escape its directory."""
    cleaned = _UNSAFE.sub("_", text or "")
    cleaned = cleaned.lstrip(".") or "unnamed"
    return cleaned[:120]


def decode_project_dir(name: str) -> str:
    """Best-effort inverse of Claude Code's cwd mangling.

    Claude Code names transcript directories by replacing every ``/`` in the
    absolute cwd with ``-``, which is lossy: ``/Users/x/my-repo`` and
    ``/Users/x/my/repo`` mangle identically. We only use the result for display
    and slug fallback; the authoritative cwd comes from the ``cwd`` field inside
    the transcript rows themselves.
    """
    return "/" + name.lstrip("-").replace("-", "/") if name.startswith("-") else name


# ---------------------------------------------------------------- project registry


def project_slug(cwd: str) -> str:
    """Stable, friendly directory name for a project.

    The basename is what a human recognises, so that is what we use. Two
    different checkouts sharing a basename would collide, so the first cwd to
    claim a slug keeps it and later ones get a short hash suffix. The claim is
    recorded in ``state/projects.json`` so the answer never changes underneath
    an existing log directory.
    """
    cwd = os.path.abspath(os.path.expanduser(cwd or "."))
    base = slugify(os.path.basename(cwd.rstrip("/")) or "root", 48)

    registry_path = state_dir() / "projects.json"
    registry = _read_json(registry_path, {})
    if cwd in registry:
        return registry[cwd]

    slug = base
    taken = set(registry.values())
    if slug in taken:
        import hashlib

        digest = hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:6]
        slug = f"{base}-{digest}"

    registry[cwd] = slug
    ensure_dirs()
    _write_json_atomic(registry_path, registry)
    return slug


def log_path(cwd: str, session_id: str, title: str, started: str) -> Path:
    """Where a session's markdown lives.

    ``<logs>/<project-slug>/<YYYY-MM-DD>-<title-slug>-<sid8>.md``. The short
    session-id suffix guarantees uniqueness without making the name unreadable,
    and keeps the file stable if the AI-generated title later changes.
    """
    day = (started or "")[:10] or "0000-00-00"
    sid8 = safe_component(session_id)[:8]
    name = f"{day}-{slugify(title, 48)}-{sid8}.md"
    return logs_dir() / project_slug(cwd) / name


# ---------------------------------------------------------------- json helpers


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, data) -> None:
    write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def write_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp file and rename.

    The daemon rewrites a log whenever the transcript grows, and the viewer or a
    text editor may be reading it at the same moment. ``rename`` within a
    directory is atomic on POSIX, so a reader sees either the old file or the
    new one and never a half-written one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_json(path, default=None):
    return _read_json(Path(path), default)


def write_json(path, data) -> None:
    _write_json_atomic(Path(path), data)
