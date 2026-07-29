"""Replay a recorded transcript as if it were happening now.

The hard part of this program to verify is the live path: does the watcher
notice, does the daemon rebuild only what changed, does the viewer append
without losing your scroll position or your open disclosure triangles. Waiting
for a real session to produce those conditions is slow and unrepeatable.

So: take a real transcript, write it into a scratch session one row at a time,
preserving the original inter-row timing scaled by ``--speed``. Everything
downstream — watcher, builder, markdown writer, SSE, viewer — sees a live
session and cannot tell the difference.

Doubles as the demo.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid as uuidlib
from pathlib import Path

from . import paths, transcript

REPLAY_CWD = os.path.join(tempfile.gettempdir(), "scribe-replay")
MAX_STEP_S = 2.5  # never make a demo sit through a real 20-minute pause


def _target_dir() -> Path:
    mangled = REPLAY_CWD.replace("/", "-")
    target = paths.projects_dir() / mangled
    target.mkdir(parents=True, exist_ok=True)
    return target


def _delay(previous: str, current: str, speed: float) -> float:
    if speed <= 0 or not previous or not current:
        return 0.0
    from .build import _elapsed_ms

    seconds = _elapsed_ms(previous, current) / 1000.0 / speed
    return min(seconds, MAX_STEP_S)


def run(source: str, speed: float = 20.0, max_rounds: int = 0) -> int:
    path = Path(source).expanduser()
    if not path.is_file():
        found = transcript.find_transcript(source)
        if found is None:
            refs = [r for r in transcript.index_sessions() if r.session_id.startswith(source)]
            if len(refs) != 1:
                sys.stderr.write(f"scribe: no transcript matching '{source}'\n")
                return 1
            found = refs[0].path
        path = found

    rows = transcript.read_all(path)
    if not rows:
        sys.stderr.write(f"scribe: {path} has no readable rows\n")
        return 1

    os.makedirs(REPLAY_CWD, exist_ok=True)
    session_id = str(uuidlib.uuid4())
    target = _target_dir() / f"{session_id}.jsonl"

    sys.stdout.write(f"replaying {len(rows)} rows at {speed}x -> session {session_id[:8]}\n")
    sys.stdout.write(f"  {target}\n")
    if speed > 0:
        sys.stdout.write("  ctrl-c to stop\n")

    now = time.time()
    rounds = 0
    previous_ts = ""
    written = 0

    try:
        with open(target, "w", encoding="utf-8") as fh:
            for row in rows:
                if max_rounds and rounds >= max_rounds:
                    break

                original_ts = row.get("timestamp") or ""
                pause = _delay(previous_ts, original_ts, speed)
                if pause > 0:
                    time.sleep(pause)
                if original_ts:
                    previous_ts = original_ts

                fresh = dict(row)
                fresh["sessionId"] = session_id
                fresh.pop("session_id", None)
                if fresh.get("cwd"):
                    fresh["cwd"] = REPLAY_CWD
                if original_ts:
                    now += pause if pause else 0.05
                    fresh["timestamp"] = (
                        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
                        + f".{int((now % 1) * 1000):03d}Z"
                    )

                fh.write(json.dumps(fresh, default=str) + "\n")
                fh.flush()  # the watcher reads by offset; buffering would stall it
                written += 1

                if row.get("type") == "user" and not row.get("isSidechain"):
                    from .build import user_prompt_text

                    if user_prompt_text(row):
                        rounds += 1
                        sys.stdout.write(f"  round {rounds}\n")
                        sys.stdout.flush()
    except KeyboardInterrupt:
        sys.stdout.write("\nstopped\n")

    sys.stdout.write(f"wrote {written} rows\n")
    return 0
