"""The archive: a permanent, byte-for-byte copy of every transcript.

This is the reason scribe exists.

Claude Code deletes transcripts older than ``cleanupPeriodDays`` — 30 by
default — and it does so silently. On the machine this was written, the sweep
had already run that morning and nothing survived from before four weeks prior.
Every other tool in this space reads ``~/.claude/projects`` and stops there, so
they all inherit that expiry. The rendered markdown is a nice artifact; the
archive is the one that means you cannot lose the conversation.

A session is four things on disk, not one::

    <project>/<session>.jsonl                      the conversation
    <project>/<session>/subagents/agent-*.jsonl    subagent conversations
    <project>/<session>/subagents/agent-*.meta.json  which Task spawned each
    <project>/<session>/tool-results/<id>.txt      outputs too large to inline

Missing any of them makes "fully reproducible" untrue, so all four are mirrored.

The copy is incremental and append-only, which matches how Claude Code writes:
we remember how many bytes we have taken and append whatever is new. Compaction
does not interfere — it appends a boundary marker and keeps going, leaving
earlier rows intact. The one case that can destroy data is a source file being
rewritten or truncated underneath us; that is rare, and it rotates the existing
archive to a generation file rather than overwriting it.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

# Sidecar directories that belong to a session, mirrored wholesale.
SIDECAR_DIRS = ("subagents", "tool-results")

# A single record beyond this is not a transcript we can meaningfully mirror.
MAX_COPY_BYTES = 512 * 1024 * 1024


@dataclass
class Stats:
    sessions: int = 0
    files: int = 0
    bytes_copied: int = 0
    rotated: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "sessions": self.sessions,
            "files": self.files,
            "bytes": self.bytes_copied,
            "rotated": self.rotated,
            "errors": self.errors,
        }


def archive_dir() -> Path:
    return paths.root() / "archive"


def state_file() -> Path:
    return archive_dir() / "state.json"


def _load_state() -> dict:
    data = paths.read_json(state_file(), {}) or {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    archive_dir().mkdir(parents=True, exist_ok=True)
    paths.write_json(state_file(), state)


# ---------------------------------------------------------------- mirroring


def _rotate(dst: Path) -> Path | None:
    """Move an archived file aside so a rewritten source cannot destroy it."""
    for n in range(1, 1000):
        candidate = dst.with_name(f"{dst.stem}.gen{n}{dst.suffix}")
        if not candidate.exists():
            try:
                dst.rename(candidate)
                return candidate
            except OSError:
                return None
    return None


def is_inside_archive(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(archive_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def mirror_file(src: Path, dst: Path, state: dict, stats: Stats) -> None:
    """Copy whatever of ``src`` we do not already have in ``dst``.

    Keyed on (inode, size) so an unchanged file costs a single ``stat``.
    """
    # Never mirror the archive onto itself. Once a session outlives its
    # original it re-enters the index pointing at the archived copy, and
    # without this the rotate-then-copy path renames the destination away and
    # then fails to read the source it just moved — losing the canonical file.
    try:
        if src.resolve() == dst.resolve():
            return
    except OSError:
        return

    key = str(src)
    try:
        st = src.stat()
    except OSError:
        return  # vanished mid-sweep; we keep whatever we already archived
    if st.st_size > MAX_COPY_BYTES:
        return

    prev = state.get(key) or {}
    prev_size = int(prev.get("size") or 0)
    prev_inode = prev.get("inode")
    have = dst.exists()

    try:
        if have and prev_inode == st.st_ino and st.st_size == prev_size:
            return  # nothing new
        dst.parent.mkdir(parents=True, exist_ok=True)

        if have and prev_inode == st.st_ino and st.st_size > prev_size:
            # The normal case: append the new tail.
            with open(src, "rb") as fh_in, open(dst, "ab") as fh_out:
                fh_in.seek(prev_size)
                shutil.copyfileobj(fh_in, fh_out)
            stats.bytes_copied += st.st_size - prev_size
        else:
            # First sight, or the source was replaced/truncated. Never
            # overwrite: an existing archive is moved aside first.
            if have:
                if _rotate(dst) is not None:
                    stats.rotated += 1
            shutil.copy2(src, dst)
            stats.bytes_copied += st.st_size

        stats.files += 1
        state[key] = {"size": st.st_size, "inode": st.st_ino, "mtime": st.st_mtime,
                      "dst": str(dst), "seen": time.time()}
    except OSError:
        stats.errors += 1


def mirror_tree(src_dir: Path, dst_dir: Path, state: dict, stats: Stats) -> None:
    if not src_dir.is_dir():
        return
    try:
        entries = sorted(src_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            mirror_tree(entry, dst_dir / entry.name, state, stats)
        elif entry.is_file():
            mirror_file(entry, dst_dir / entry.name, state, stats)


# ---------------------------------------------------------------- sessions


def session_archive_path(project_slug: str, session_id: str) -> Path:
    return archive_dir() / paths.safe_component(project_slug) / f"{paths.safe_component(session_id)}.jsonl"


def archive_ref(ref, state: dict, stats: Stats) -> None:
    """Mirror one session: its transcript and every sidecar it owns."""
    from . import transcript as _t

    if ref.cwd and _t.is_explainer_cwd(ref.cwd, str(paths.root())):
        return
    if getattr(ref, "archived", False) or is_inside_archive(Path(ref.path)):
        return  # already the archived copy; there is nothing upstream to take

    slug = paths.project_slug(ref.cwd) if ref.cwd else paths.safe_component(ref.project_dir)
    target = session_archive_path(slug, ref.session_id)
    mirror_file(Path(ref.path), target, state, stats)

    sidecar_src = Path(ref.path).with_suffix("")
    if sidecar_src.is_dir():
        sidecar_dst = target.with_suffix("")
        for name in SIDECAR_DIRS:
            mirror_tree(sidecar_src / name, sidecar_dst / name, state, stats)
    stats.sessions += 1


def sweep(refs=None, save: bool = True) -> Stats:
    """Archive every visible session. Cheap when nothing has changed."""
    from . import transcript as _t

    paths.ensure_dirs()
    archive_dir().mkdir(parents=True, exist_ok=True)
    state = _load_state()
    stats = Stats()
    for ref in refs if refs is not None else _t.index_sessions():
        try:
            archive_ref(ref, state, stats)
        except Exception:
            stats.errors += 1
    if save and (stats.files or stats.rotated):
        _save_state(state)
    return stats


def archive_one(ref) -> Stats:
    """Archive a single session immediately (used on hook pokes)."""
    paths.ensure_dirs()
    archive_dir().mkdir(parents=True, exist_ok=True)
    state = _load_state()
    stats = Stats()
    try:
        archive_ref(ref, state, stats)
    except Exception:
        stats.errors += 1
    if stats.files or stats.rotated:
        _save_state(state)
    return stats


# ---------------------------------------------------------------- reading back


def iter_archived(projects_root: Path | None = None):
    """Every archived transcript, as ``(project_slug, path)``.

    Generation files (``*.gen1.jsonl``) are surfaced too: they are earlier
    incarnations of a rewritten session and are still real history.
    """
    root = Path(projects_root or archive_dir())
    if not root.is_dir():
        return
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        for jsonl in sorted(project.glob("*.jsonl")):
            yield project.name, jsonl


def summary() -> dict:
    """Counts for `scribe status`."""
    total_files = 0
    total_bytes = 0
    sessions = 0
    root = archive_dir()
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and path.name != "state.json":
                total_files += 1
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    pass
        sessions = sum(1 for _ in iter_archived())
    return {"sessions": sessions, "files": total_files, "bytes": total_bytes,
            "path": str(root)}


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"
