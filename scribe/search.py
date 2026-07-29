"""Full-text search across every conversation.

An archive you cannot search is a landfill. This indexes every session — live
and archived alike — so "which conversation was it where I fixed the reveal.js
fragment bug?" is answerable in milliseconds instead of by grepping 134 MB.

Built on SQLite's FTS5, which ships inside the standard library's ``sqlite3``.
That buys ranked results (``bm25``) and highlighted context (``snippet``) with
no dependency and no separate index format to maintain.

Granularity is one document per *item*, not per session: a prompt, a reply
paragraph, a thought, or a tool call. Coarser than that and a hit in a
2,000-round session tells you nothing about where to look; finer and the
snippets lose their context.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

# Bounded so a single `cat` of a large file cannot dominate the index. The
# markdown renderer uses the same ceiling for the same reason.
MAX_DOC_CHARS = 4000


def db_path() -> Path:
    return paths.root() / "index.db"


# ---------------------------------------------------------------- query parsing

_TERM = re.compile(r'"([^"]+)"|(\S+)')
_WORDY = re.compile(r"[^\w*]+", re.UNICODE)


def fts_query(text: str) -> str:
    """Turn what someone typed into a valid FTS5 MATCH expression.

    Users type `foo bar`, `"exact phrase"`, `rev*`, and also things like
    `rm -rf` or `a:b` that are FTS5 operators and would otherwise raise a syntax
    error mid-keystroke. Every term is quoted, which makes it a literal phrase,
    and a trailing `*` is preserved as a prefix match.
    """
    parts: list[str] = []
    for phrase, bare in _TERM.findall(text or ""):
        token = (phrase or bare or "").strip()
        if not token:
            continue
        prefix = token.endswith("*")
        cleaned = _WORDY.sub(" ", token).strip().rstrip("*").strip()
        if not cleaned:
            continue
        escaped = cleaned.replace('"', '""')
        parts.append(f'"{escaped}"*' if prefix else f'"{escaped}"')
    return " ".join(parts)


# ---------------------------------------------------------------- the index


class SearchIndex:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or db_path())
        self._lock = threading.Lock()
        self._local = threading.local()
        self.syncing = False
        self.last_sync: dict = {}
        self._ensure_schema()

    # -- connection -----------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        conn = self._conn()
        with self._lock:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version and version != SCHEMA_VERSION:
                # The index is derived data; rebuilding is always safe and
                # cheaper than migrating.
                conn.executescript("DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS sources;")
                version = 0
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    session_id TEXT PRIMARY KEY,
                    path       TEXT,
                    size       INTEGER,
                    mtime      REAL,
                    project    TEXT,
                    title      TEXT,
                    cwd        TEXT,
                    updated    TEXT,
                    archived   INTEGER DEFAULT 0,
                    rounds     INTEGER DEFAULT 0,
                    indexed_at REAL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
                    session_id UNINDEXED,
                    round_index UNINDEXED,
                    ts UNINDEXED,
                    kind UNINDEXED,
                    body,
                    tokenize = "unicode61 remove_diacritics 2"
                );
                """
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()

    # -- building -------------------------------------------------------

    def needs_reindex(self, ref) -> bool:
        row = self._conn().execute(
            "SELECT size, mtime FROM sources WHERE session_id=?", (ref.session_id,)
        ).fetchone()
        if row is None:
            return True
        return int(row["size"] or 0) != int(ref.size or 0) or abs(
            float(row["mtime"] or 0) - float(ref.mtime or 0)
        ) > 0.001

    def index_ref(self, ref, session=None) -> int:
        """(Re)index one session. Returns the number of documents written."""
        from .build import build_from_path

        if session is None:
            session = build_from_path(ref.path, cwd_hint=ref.cwd)
        docs = list(_documents(session))
        conn = self._conn()
        with self._lock:
            conn.execute("DELETE FROM docs WHERE session_id=?", (ref.session_id,))
            conn.executemany(
                "INSERT INTO docs(session_id, round_index, ts, kind, body) VALUES (?,?,?,?,?)",
                [(ref.session_id, d[0], d[1], d[2], d[3]) for d in docs],
            )
            conn.execute(
                """INSERT INTO sources(session_id, path, size, mtime, project, title, cwd,
                                       updated, archived, rounds, indexed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     path=excluded.path, size=excluded.size, mtime=excluded.mtime,
                     project=excluded.project, title=excluded.title, cwd=excluded.cwd,
                     updated=excluded.updated, archived=excluded.archived,
                     rounds=excluded.rounds, indexed_at=excluded.indexed_at""",
                (
                    ref.session_id, str(ref.path), int(ref.size or 0), float(ref.mtime or 0),
                    paths.project_slug(ref.cwd) if ref.cwd else ref.project_dir,
                    session.title or ref.title, ref.cwd, ref.updated or session.updated,
                    1 if getattr(ref, "archived", False) else 0,
                    len(session.rounds), time.time(),
                ),
            )
            conn.commit()
        return len(docs)

    def sync(self, refs=None, force: bool = False) -> dict:
        """Bring the index up to date. Only changed sessions are re-read."""
        from . import transcript as _t

        refs = list(refs) if refs is not None else _t.index_sessions()
        started = time.time()
        self.syncing = True
        indexed = docs = skipped = failed = 0
        try:
            for ref in refs:
                if not force and not self.needs_reindex(ref):
                    skipped += 1
                    continue
                try:
                    docs += self.index_ref(ref)
                    indexed += 1
                except Exception:
                    failed += 1
            self._prune(refs)
        finally:
            self.syncing = False
        self.last_sync = {
            "indexed": indexed, "skipped": skipped, "failed": failed,
            "documents": docs, "seconds": round(time.time() - started, 2),
        }
        return self.last_sync

    def _prune(self, refs) -> None:
        """Drop sessions that no longer exist anywhere."""
        alive = {r.session_id for r in refs}
        conn = self._conn()
        with self._lock:
            known = {r["session_id"] for r in conn.execute("SELECT session_id FROM sources")}
            for stale in known - alive:
                conn.execute("DELETE FROM docs WHERE session_id=?", (stale,))
                conn.execute("DELETE FROM sources WHERE session_id=?", (stale,))
            if known - alive:
                conn.commit()

    # -- querying -------------------------------------------------------

    def search(self, text: str, limit: int = 300, per_session: int = 4) -> dict:
        """Ranked matches, grouped by conversation.

        Grouped because the question is "which conversation was that in?" —
        a flat list of 300 hits from one noisy session buries the answer.
        """
        match = fts_query(text)
        if not match:
            return {"query": text, "sessions": [], "total": 0, "truncated": False}

        conn = self._conn()
        try:
            rows = conn.execute(
                """SELECT d.session_id, d.round_index, d.ts, d.kind,
                          snippet(docs, 4, '\x02', '\x03', '…', 14) AS snip,
                          bm25(docs) AS rank
                   FROM docs d
                   WHERE docs MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"query": text, "sessions": [], "total": 0, "error": "could not parse that query"}

        meta = {
            r["session_id"]: dict(r)
            for r in conn.execute("SELECT * FROM sources")
        }

        grouped: dict[str, dict] = {}
        for row in rows:
            sid = row["session_id"]
            info = meta.get(sid)
            if info is None:
                continue
            entry = grouped.setdefault(sid, {
                "id": sid,
                "title": info.get("title") or sid[:8],
                "project": info.get("project") or "",
                "updated": info.get("updated") or "",
                "archived": bool(info.get("archived")),
                "path": info.get("path") or "",
                "hits": 0,
                "matches": [],
                "rank": row["rank"],
            })
            entry["hits"] += 1
            entry["rank"] = min(entry["rank"], row["rank"])
            if len(entry["matches"]) < per_session:
                entry["matches"].append({
                    "round": row["round_index"],
                    "ts": row["ts"],
                    "kind": row["kind"],
                    "snippet": row["snip"],
                })

        sessions = sorted(grouped.values(), key=lambda s: (s["rank"], -s["hits"]))
        return {
            "query": text,
            "match": match,
            "sessions": sessions,
            "total": sum(s["hits"] for s in sessions),
            "truncated": len(rows) >= limit,
        }

    def stats(self) -> dict:
        conn = self._conn()
        try:
            sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        except sqlite3.OperationalError:
            sources = docs = 0
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"sessions": sources, "documents": docs, "bytes": size,
                "path": str(self.path), "syncing": self.syncing,
                "last_sync": self.last_sync}


# ---------------------------------------------------------------- documents


def _documents(session):
    """Yield (round_index, ts, kind, body) for everything worth searching."""
    from .model import Notice, Text, Thinking, ToolCall

    def emit(rounds, prefix=""):
        for rnd in rounds:
            if rnd.prompt:
                yield rnd.index, rnd.ts, prefix + "prompt", rnd.prompt[:MAX_DOC_CHARS]
            for item in rnd.items:
                if isinstance(item, Text):
                    if item.md.strip():
                        yield rnd.index, item.ts, prefix + "reply", item.md[:MAX_DOC_CHARS]
                elif isinstance(item, Thinking):
                    if item.md.strip():
                        yield rnd.index, item.ts, prefix + "thinking", item.md[:MAX_DOC_CHARS]
                elif isinstance(item, Notice):
                    if item.text.strip():
                        yield rnd.index, item.ts, prefix + "notice", item.text[:500]
                elif isinstance(item, ToolCall):
                    body = _tool_text(item)
                    if body.strip():
                        yield rnd.index, item.ts, prefix + "tool", body[:MAX_DOC_CHARS]
                    if item.subagent:
                        yield from emit(item.subagent, prefix="subagent:")

    yield from emit(session.rounds)


def _tool_text(call) -> str:
    data = call.input if isinstance(call.input, dict) else {}
    parts = [call.name, call.subject or ""]
    for key in ("command", "pattern", "query", "url", "description", "prompt", "file_path"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if call.explanation:
        parts.append(call.explanation)
    for chunk in (call.stdout, call.stderr, call.result_text):
        if chunk and chunk.strip():
            parts.append(chunk[:MAX_DOC_CHARS])
            break
    return "\n".join(parts)


# ---------------------------------------------------------------- singleton

_INDEX: SearchIndex | None = None
_INDEX_LOCK = threading.Lock()


def get_index() -> SearchIndex:
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = SearchIndex()
        return _INDEX


def reset() -> None:
    """Drop the cached singleton (tests relocate SCRIBE_HOME between cases)."""
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None
