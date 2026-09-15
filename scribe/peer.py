"""Delivering a message straight into a running Claude Code session.

Claude Code 2.1 gives every session an inbox: a Unix socket it registers in
``~/.claude/sessions/<pid>.json`` next to a ``<pid>.<hash>.key`` file holding
the token a peer must present. Claude Code built it so one session can message
another, and a line of JSON on that socket lands exactly as a prompt typed at
the terminal: it starts a turn when Claude is idle and waits its turn when
Claude is busy. That makes it the right channel for the composer. Unlike the
Stop-hook queue it needs no hooks, works while Claude is waiting for you, and
the message reaches the transcript as an ordinary user row, so the log stays a
pure function of the file.

The protocol is newline-delimited JSON, one connection per message::

    {"type": "auth", "token": "<peerToken from the .key file>"}
    {"type": "user", "message": {"role": "user", "content": "..."}}

The content is wrapped in Claude Code's own ``<cross-session-message>``
envelope. That is what makes the transcript row carry ``origin.name`` and a
clean ``origin.body``, which is how the builder tells a message typed on the
scribe page from one sent by another Claude session, without parsing prose.

The envelope deliberately asserts no permission mode. Claude Code's parity
check holds a message from a sender that asserts none when the recipient runs
with permissions bypassed (``bypassPermissions`` or auto mode), and asks in the
terminal before delivering it. That check is what stops a less trusted process
from steering a more trusted session, and scribe is exactly such a process as
far as Claude Code can tell, so it does not claim otherwise. A user who wants
page messages to land in bypass-mode sessions without the prompt has Claude
Code's own switch for it: ``"crossSessionInbound": "accept"`` in settings.

The token is read at send time and never stored, logged, or sent anywhere but
the socket it was published for.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path

from . import paths

CONNECT_TIMEOUT = 2.0
#: How long to listen for a receipt after the message is written. The inbox
#: usually answers within milliseconds; silence is not failure.
REPLY_WAIT = 1.5

#: The name the envelope carries. The builder keys on it (``origin.name``).
SENDER_NAME = "scribe"
ENVELOPE_TAG = "cross-session-message"
#: Permission modes under which Claude Code holds an inbound message for
#: approval in the terminal instead of delivering it straight away.
HELD_MODES = ("bypassPermissions", "auto")


def envelope(text: str, name: str = SENDER_NAME) -> str:
    """Wrap ``text`` exactly as Claude Code's own sender would.

    The receiver re-serialises what it parsed and compares byte for byte, so
    the attribute order and the newlines are not negotiable. A closing tag
    inside the body is defused the same way Claude Code does it.
    """
    clean_name = "".join(ch for ch in name if ch not in '"<>').strip()
    head = f' from-name="{clean_name}"' if clean_name else ""
    body = text.replace(f"</{ENVELOPE_TAG}", "<\\")
    return f"<{ENVELOPE_TAG}{head}>\n{body}\n</{ENVELOPE_TAG}>"


@dataclass
class Peer:
    """A live session's inbox, as published in the registry."""

    session_id: str
    pid: int
    socket_path: str
    cwd: str = ""
    kind: str = ""
    name: str = ""
    status: str = ""
    proc_start: str = ""
    key_path: str = ""

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
        }


def sessions_dir() -> Path:
    return paths.claude_home() / "sessions"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def registry() -> dict[str, Peer]:
    """Every session on this machine that currently has an inbox, by id.

    A record whose process is gone, whose socket is missing, or whose key
    file's ``procStart`` disagrees with the record (a reused pid) is skipped:
    delivering to the wrong process is worse than not delivering.
    """
    folder = sessions_dir()
    peers: dict[str, Peer] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return peers
    keys: dict[int, str] = {}
    for name in names:
        if name.endswith(".key") and "." in name[:-4]:
            head = name.split(".", 1)[0]
            if head.isdigit():
                keys[int(head)] = str(folder / name)
    for name in names:
        if not name.endswith(".json"):
            continue
        record = paths.read_json(folder / name, None)
        if not isinstance(record, dict):
            continue
        try:
            pid = int(record.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        sid = str(record.get("sessionId") or "")
        sock = str(record.get("messagingSocketPath") or "")
        if not sid or not sock or not _alive(pid):
            continue
        if not os.path.exists(sock):
            continue
        peers[sid] = Peer(
            session_id=sid,
            pid=pid,
            socket_path=sock,
            cwd=str(record.get("cwd") or ""),
            kind=str(record.get("kind") or ""),
            name=str(record.get("name") or ""),
            status=str(record.get("status") or ""),
            proc_start=str(record.get("procStart") or ""),
            key_path=keys.get(pid, ""),
        )
    return peers


def lookup(session_id: str) -> Peer | None:
    return registry().get(session_id)


def _token(peer: Peer) -> str:
    if not peer.key_path:
        return ""
    data = paths.read_json(Path(peer.key_path), None)
    if not isinstance(data, dict):
        return ""
    if peer.proc_start and data.get("procStart") and data.get("procStart") != peer.proc_start:
        return ""
    return str(data.get("peerToken") or "")


def send(peer: Peer, text: str, timeout: float = REPLY_WAIT) -> dict:
    """Put ``text`` in front of the session. Returns a small result dict.

    ``ok`` means the inbox accepted the connection and read the message; any
    receipt frames are passed through as ``receipts``. A refusal is reported
    as ``error`` with the reason the inbox gave, or ours if it never got that
    far.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    token = _token(peer)
    if not token:
        return {"ok": False, "error": "no inbox key for this session"}

    msg_id = "cc-msg-" + uuidlib.uuid4().hex
    frames = [
        {"type": "auth", "token": token},
        {
            "type": "user",
            "msg_id": msg_id,
            "session_id": peer.session_id,
            "message": {"role": "user", "content": envelope(text)},
        },
    ]
    payload = "".join(json.dumps(f) + "\n" for f in frames).encode("utf-8")

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except (AttributeError, OSError) as exc:
        return {"ok": False, "error": f"no unix sockets: {exc}"}
    receipts: list = []
    try:
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(peer.socket_path)
        sock.sendall(payload)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        sock.settimeout(0.25)
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    receipts.append(json.loads(line.decode("utf-8", "replace")))
                except ValueError:
                    receipts.append({"raw": line.decode("utf-8", "replace")[:200]})
    except (OSError, socket.timeout) as exc:
        return {"ok": False, "error": f"inbox unreachable: {exc}", "msg_id": msg_id}
    finally:
        try:
            sock.close()
        except OSError:
            pass

    result = {"ok": True, "msg_id": msg_id, "receipts": receipts}
    for frame in receipts:
        if not isinstance(frame, dict):
            continue
        reason = frame.get("drop_reason") or frame.get("error")
        if reason:
            result["ok"] = False
            result["error"] = str(reason)
        status = frame.get("status")
        if status:
            result["status"] = str(status)
    return result


# ------------------------------------------------------------------ children


def child_env() -> dict:
    """The environment for a Claude Code child that must be its own session.

    The daemon is usually started from a hook, inside a session, and inherits
    that session's identity: its id, its inbox socket and token, the nesting
    marker. A child that sees those thinks it is part of the parent. Only the
    config-dir override is kept, because tests and multi-profile setups rely on
    it, and ``SCRIBE_DISABLE`` is dropped so our hooks record the new turn.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("CLAUDE") and k != "CLAUDE_CONFIG_DIR")
    }
    env.pop("SCRIBE_DISABLE", None)
    return env
