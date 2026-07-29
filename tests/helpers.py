"""Shared test scaffolding: an isolated scribe home and synthetic transcripts."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Isolated(unittest.TestCase):
    """Points SCRIBE_HOME and CLAUDE_CONFIG_DIR at throwaway directories.

    Every test that touches disk inherits from this, so a test run can never
    read or write the developer's real logs, transcripts, or settings.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scribe-test-"))
        self._env = {}
        for key, value in {
            "SCRIBE_HOME": str(self.tmp / "home"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
        }.items():
            self._env[key] = os.environ.get(key)
            os.environ[key] = value
        (self.tmp / "claude" / "projects").mkdir(parents=True, exist_ok=True)
        from scribe import paths

        paths.ensure_dirs()

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- transcript authoring -------------------------------------------

    def transcript_path(self, cwd: str = "/tmp/proj", session_id: str | None = None) -> Path:
        from scribe import paths

        session_id = session_id or str(uuid.uuid4())
        folder = paths.projects_dir() / cwd.replace("/", "-")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{session_id}.jsonl"

    def write_rows(self, path: Path, rows: list[dict]) -> Path:
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return path

    def append_rows(self, path: Path, rows: list[dict]) -> Path:
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
            fh.flush()
        return path


# ---------------------------------------------------------------- builders


def base(session_id: str, cwd: str = "/tmp/proj", **extra) -> dict:
    row = {
        "sessionId": session_id,
        "cwd": cwd,
        "version": "2.1.220",
        "gitBranch": "main",
        "isSidechain": False,
        "userType": "external",
    }
    row.update(extra)
    return row


def user_row(session_id: str, text: str, ts: str, uuid_: str = "u1", cwd: str = "/tmp/proj", **extra) -> dict:
    return base(
        session_id,
        cwd,
        type="user",
        uuid=uuid_,
        timestamp=ts,
        message={"role": "user", "content": text},
        **extra,
    )


def assistant_row(session_id: str, blocks: list, ts: str, uuid_: str = "a1", usage=None, cwd: str = "/tmp/proj", **extra) -> dict:
    return base(
        session_id,
        cwd,
        type="assistant",
        uuid=uuid_,
        timestamp=ts,
        message={
            "role": "assistant",
            "model": "claude-opus-5",
            "content": blocks,
            "usage": usage or {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 500},
        },
        **extra,
    )


def tool_use(call_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def tool_result_row(session_id: str, call_id: str, content, ts: str, sidecar=None, is_error=False, uuid_="r1", cwd: str = "/tmp/proj") -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error:
        block["is_error"] = True
    row = base(
        session_id,
        cwd,
        type="user",
        uuid=uuid_,
        timestamp=ts,
        message={"role": "user", "content": [block]},
    )
    if sidecar is not None:
        row["toolUseResult"] = sidecar
    return row


def simple_session(session_id: str = "sess-1", cwd: str = "/tmp/proj") -> list[dict]:
    """One round: a prompt, some thinking, a Bash call with output, a reply."""
    return [
        {"type": "ai-title", "aiTitle": "Check the build", "sessionId": session_id},
        user_row(session_id, "Please check the build.", "2026-07-28T10:00:00.000Z", "u1", cwd=cwd),
        assistant_row(
            session_id,
            [
                {"type": "thinking", "thinking": "I should run the tests first."},
                {"type": "text", "text": "Running the tests."},
                tool_use("toolu_A", "Bash", {"command": "pytest -q", "description": "run tests"}),
            ],
            "2026-07-28T10:00:20.000Z",
            "a1",
            cwd=cwd,
        ),
        tool_result_row(
            session_id,
            "toolu_A",
            "5 passed",
            "2026-07-28T10:00:25.000Z",
            sidecar={"stdout": "5 passed", "stderr": "", "interrupted": False},
            cwd=cwd,
        ),
        assistant_row(
            session_id,
            [{"type": "text", "text": "All **5** tests pass."}],
            "2026-07-28T10:00:30.000Z",
            "a2",
            cwd=cwd,
        ),
    ]
