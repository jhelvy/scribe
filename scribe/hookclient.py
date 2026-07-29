"""The hook entry point: a deliberately tiny Unix-socket client.

Every hook Claude Code fires runs a process. That process is in the critical
path of your session — a slow ``PostToolUse`` hook is latency you feel on every
tool call, and a slow ``PermissionRequest`` hook is a terminal that appears to
hang. So this module does as close to nothing as possible: read stdin, hand the
payload to the daemon, print whatever comes back, exit 0.

All the actual behaviour lives in :func:`scribe.daemon.dispatch`, which means
hook logic can change without reinstalling anything.

Failure is always silent and always successful. If the daemon is not running,
there is nothing to lose by giving up: the transcript is the source of truth and
the daemon reconstructs everything from it whenever it next starts.
"""

from __future__ import annotations

import json
import os
import socket
import sys

# Long enough that a legitimately held approval is not cut off, short enough
# that a wedged daemon cannot hang a session forever.
DEFAULT_TIMEOUT = 300.0
CONNECT_TIMEOUT = 1.5


def socket_path() -> str:
    # sockpath is a leaf module with no package imports of its own, so this
    # stays cheap while guaranteeing the daemon and the hook agree.
    from .sockpath import control_socket_path

    return control_socket_path()


def send(payload: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """One request, one reply. Returns {} on any failure."""
    path = socket_path()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except (AttributeError, OSError):
        return {}  # no AF_UNIX (Windows): capture still works via the watcher
    try:
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(path)
        sock.settimeout(timeout)
        sock.sendall((json.dumps(payload, default=str) + "\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
        raw = b"".join(chunks).strip()
    except (OSError, socket.timeout, ValueError):
        return {}
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not raw:
        return {}
    try:
        reply = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("SCRIBE_DISABLE"):
        return 0

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return 0
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name") or ""

    reply = send(payload)

    # SessionStart is the one event that may start the daemon. Doing it here
    # rather than on every event means at most one spawn attempt per session,
    # and it is the event whose latency nobody notices.
    if not reply and event == "SessionStart":
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from scribe.daemon import ensure_running

            if ensure_running(open_browser=False):
                reply = send(payload, timeout=5.0)
        except Exception:
            reply = {}

    if reply:
        sys.stdout.write(json.dumps(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
