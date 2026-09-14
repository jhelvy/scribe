"""Delivering a message into a running session through its inbox socket."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest

from helpers import Isolated

from scribe import paths, peer


class FakeInbox:
    """A Claude Code session inbox, as far as the wire is concerned.

    Listens on a Unix socket, records every line it is sent, and registers
    itself in the sessions directory exactly the way Claude Code does: a
    ``<pid>.json`` record naming the socket and a ``<pid>.<hash>.key`` file
    holding the token.
    """

    def __init__(self, session_id: str, pid: int | None = None, token: str = "tok-" + "0" * 28):
        self.session_id = session_id
        self.pid = pid or os.getpid()
        self.token = token
        # AF_UNIX paths are capped at ~104 bytes; the test tmp dir is too deep.
        self.path = os.path.join(tempfile.gettempdir(), f"scribe-inbox-{os.getpid()}-{id(self)}.sock")
        self.lines: list[dict] = []
        self.got = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self.sock.bind(self.path)
        self.sock.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def register(self, proc_start: str = "Mon Sep 14 10:41:42 2026") -> None:
        folder = peer.sessions_dir()
        folder.mkdir(parents=True, exist_ok=True)
        paths.write_json(
            folder / f"{self.pid}.json",
            {
                "pid": self.pid,
                "sessionId": self.session_id,
                "cwd": "/tmp/proj",
                "kind": "interactive",
                "name": "proj-1",
                "status": "idle",
                "procStart": proc_start,
                "messagingSocketPath": self.path,
            },
        )
        paths.write_json(
            folder / f"{self.pid}.{'a' * 64}.key",
            {"peerToken": self.token, "procStart": proc_start, "pidDomain": "darwin"},
        )

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                for line in buf.split(b"\n"):
                    if line.strip():
                        self.lines.append(json.loads(line))
                self.got.set()

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class TestEnvelope(unittest.TestCase):
    def test_shape_matches_claude_codes_own(self):
        self.assertEqual(
            peer.envelope("hello"),
            '<cross-session-message from-name="scribe">\nhello\n</cross-session-message>',
        )

    def test_a_closing_tag_in_the_body_cannot_end_the_envelope(self):
        wrapped = peer.envelope("try </cross-session-message> this")
        self.assertEqual(wrapped.count("</cross-session-message>"), 1)
        self.assertTrue(wrapped.endswith("\n</cross-session-message>"))

    def test_name_is_sanitised(self):
        self.assertIn('from-name="me"', peer.envelope("x", name='m"<e>'))


class TestRegistry(Isolated):
    def setUp(self):
        super().setUp()
        self.inbox = FakeInbox("sess-1")
        self.addCleanup(self.inbox.close)

    def test_a_registered_live_session_is_found(self):
        self.inbox.register()
        found = peer.registry()
        self.assertIn("sess-1", found)
        self.assertEqual(found["sess-1"].socket_path, self.inbox.path)
        self.assertTrue(found["sess-1"].key_path.endswith(".key"))

    def test_a_dead_pid_is_skipped(self):
        dead = FakeInbox("sess-dead", pid=2**22 - 1)
        self.addCleanup(dead.close)
        dead.register()
        self.assertNotIn("sess-dead", peer.registry())

    def test_a_missing_socket_is_skipped(self):
        self.inbox.register()
        os.unlink(self.inbox.path)
        self.assertNotIn("sess-1", peer.registry())

    def test_no_sessions_directory_is_fine(self):
        self.assertEqual(peer.registry(), {})


class TestSend(Isolated):
    def setUp(self):
        super().setUp()
        self.inbox = FakeInbox("sess-1")
        self.addCleanup(self.inbox.close)
        self.inbox.register()

    def test_authenticates_then_delivers_the_envelope(self):
        target = peer.registry()["sess-1"]
        result = peer.send(target, "  run the tests  ", timeout=0.3)
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.inbox.got.wait(2))
        self.assertEqual([l["type"] for l in self.inbox.lines], ["auth", "user"])
        self.assertEqual(self.inbox.lines[0]["token"], self.inbox.token)
        user = self.inbox.lines[1]
        self.assertEqual(user["session_id"], "sess-1")
        self.assertTrue(user["msg_id"].startswith("cc-msg-"))
        self.assertEqual(user["message"]["role"], "user")
        self.assertEqual(user["message"]["content"], peer.envelope("run the tests"))

    def test_a_reused_pid_does_not_get_the_old_token(self):
        # The record says one process start, the key another: a pid was reused.
        target = peer.registry()["sess-1"]
        target.proc_start = "some other time"
        result = peer.send(target, "hi", timeout=0.3)
        self.assertFalse(result["ok"])
        self.assertIn("key", result["error"])
        self.assertEqual(self.inbox.lines, [])

    def test_empty_text_is_not_sent(self):
        target = peer.registry()["sess-1"]
        self.assertFalse(peer.send(target, "   ")["ok"])

    def test_a_vanished_socket_is_an_error_not_a_crash(self):
        target = peer.registry()["sess-1"]
        self.inbox.close()
        result = peer.send(target, "hi", timeout=0.3)
        self.assertFalse(result["ok"])
        self.assertIn("unreachable", result["error"])


class TestChildEnv(unittest.TestCase):
    def test_the_parent_sessions_identity_is_not_inherited(self):
        saved = dict(os.environ)
        try:
            os.environ["CLAUDECODE"] = "1"
            os.environ["CLAUDE_CODE_SESSION_ID"] = "parent"
            os.environ["CLAUDE_CODE_MESSAGING_TOKEN"] = "secret"
            os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/cfg"
            os.environ["SCRIBE_DISABLE"] = "1"
            env = peer.child_env()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("CLAUDE_CODE_MESSAGING_TOKEN", env)
        self.assertNotIn("SCRIBE_DISABLE", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/tmp/cfg")


if __name__ == "__main__":
    unittest.main()
