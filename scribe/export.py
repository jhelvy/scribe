"""Export a session as one self-contained HTML file.

The markdown log is the artifact you keep; this is the one you *send*. Every
stylesheet, script and byte of session data is inlined, so the result opens from
a file:// URL, survives being emailed, and needs neither the daemon nor a
network. The same viewer code runs — it just notices `SCRIBE_SNAPSHOT` and
skips the parts that would talk to a server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, redact, render_json
from .daemon import VIEWER_DIR, _key_round
from .model import Session

# Matches the tags the daemon serves from /static, which is what we replace
# with inline content.
SCRIPT_TAG = re.compile(r'<script src="/static/([^"]+)"></script>')
STYLE_TAG = re.compile(r'<link rel="stylesheet" href="/static/([^"]+)" />')


def build_snapshot(session: Session, cfg: dict | None = None) -> dict:
    cfg = cfg or config.load()
    renderer = render_json.JsonRenderer(redact.from_config(cfg))
    return {
        "head": renderer.session(session, include_rounds=False),
        "rounds": [_key_round(renderer.round(r)) for r in session.rounds],
        "pending": [],
    }


def render_standalone(session: Session, cfg: dict | None = None) -> str:
    html = (VIEWER_DIR / "index.html").read_text(encoding="utf-8")

    def inline_style(match: re.Match) -> str:
        body = (VIEWER_DIR / match.group(1)).read_text(encoding="utf-8")
        return "<style>\n" + body + "\n</style>"

    def inline_script(match: re.Match) -> str:
        body = (VIEWER_DIR / match.group(1)).read_text(encoding="utf-8")
        # A literal "</script>" inside a script body would close the tag early.
        return "<script>\n" + body.replace("</script>", "<\\/script>") + "\n</script>"

    html = STYLE_TAG.sub(inline_style, html)
    html = SCRIPT_TAG.sub(inline_script, html)

    snapshot = json.dumps(build_snapshot(session, cfg), default=str)
    snapshot = snapshot.replace("</", "<\\/")  # same tag-breaking hazard
    payload = "<script>window.SCRIBE_SNAPSHOT = " + snapshot + ";</script>"

    title = _escape(session.title or "scribe session")
    html = html.replace("<title>scribe</title>", f"<title>{title} · scribe</title>")
    # After the inlined app script, so the snapshot exists before boot runs.
    return html.replace("</body>", "  " + payload + "\n  </body>")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_standalone(session: Session, target, cfg: dict | None = None) -> Path:
    target = Path(target).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_standalone(session, cfg), encoding="utf-8")
    return target
