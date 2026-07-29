"""Turning a transcript into an on-disk markdown log.

One place decides where a session's log lives and what goes in it, so the CLI,
the daemon and the tests can never disagree about it.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import build, config, explain, paths, redact, render_md
from .model import Session
from .transcript import SessionRef, peek


def annotate(session: Session) -> Session:
    """Fill in the derived fields the renderers want."""
    if session.cwd:
        session.project = paths.project_slug(session.cwd)
    session.log_path = str(log_path_for(session))
    return session


def log_path_for(session: Session) -> Path:
    return paths.log_path(
        cwd=session.cwd or os.getcwd(),
        session_id=session.id or "unknown",
        title=session.title or "session",
        started=session.started or session.updated or "",
    )


def load(path, cfg: dict | None = None, explainer=None) -> Session:
    session = build.build_from_path(path)
    annotate(session)
    explain.attach(session, explainer, cfg)
    return session


def write_markdown(session: Session, cfg: dict | None = None) -> Path | None:
    cfg = cfg or config.load()
    if not (cfg.get("markdown") or {}).get("enabled", True):
        return None
    text = render_md.render(session, cfg, redact.from_config(cfg))
    target = Path(session.log_path or log_path_for(session))
    paths.ensure_dirs()
    existing = _read(target)
    if existing == text:
        return target  # nothing changed; leave the mtime alone
    paths.write_atomic(target, text)
    return target


def _read(path: Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def build_one(transcript_path, cfg: dict | None = None, explainer=None) -> tuple[Session, Path | None]:
    """Render one transcript to markdown.

    Pass ``explainer`` when building many sessions in a row: constructing one
    re-reads the explanation cache from disk, and doing that once per session
    dominates a bulk run.
    """
    cfg = cfg or config.load()
    session = load(transcript_path, cfg, explainer)
    return session, write_markdown(session, cfg)


def prune_stale_logs(session: Session) -> None:
    """Remove earlier logs for the same session id.

    The filename embeds the AI-generated title, which Claude Code refines as a
    conversation develops. Without this a long session leaves a trail of
    near-duplicate files named after its first topic.
    """
    target = Path(session.log_path or log_path_for(session))
    suffix = f"-{paths.safe_component(session.id)[:8]}.md"
    try:
        for sibling in target.parent.glob(f"*{suffix}"):
            if sibling != target:
                sibling.unlink()
    except OSError:
        pass
