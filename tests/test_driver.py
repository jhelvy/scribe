"""The driver: a headless Claude Code child spoken to over stream-json.

Runs against `tests/fake_claude.py`, a stand-in that speaks the wire the way
2.1.272 does (see the docstring of `scribe/driver.py` for what was checked
against the real thing). The contract under test is what the driver writes,
what it makes of the frames coming back, and how it behaves when the child
is slow, asks for permission, or dies.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

from helpers import Isolated

from scribe import driver

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_claude.py"


def make_fake_binary(folder: Path) -> Path:
    """A wrapper so the fake runs under this interpreter, whatever PATH says."""
    wrapper = folder / "fake-claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


class DriverHarness(Isolated):
    def setUp(self):
        super().setUp()
        self.binary = make_fake_binary(self.tmp)
        self.log_path = self.tmp / "fake.log"
        # Resolved: the child reports its cwd with symlinks followed
        # (/var -> /private/var on macOS), and so does a real transcript.
        self.cwd = Path(os.path.realpath(self.tmp)) / "proj"
        self.cwd.mkdir()
        self.events: list[tuple[str, dict]] = []
        self.decision = {"behavior": "allow"}
        self.permissions: list[dict] = []
        self.drivers: list[driver.Driver] = []

    def tearDown(self):
        for drv in self.drivers:
            drv.stop(grace=1.0)
        super().tearDown()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()}
        env["FAKE_CLAUDE_LOG"] = str(self.log_path)
        env.update(extra)
        return env

    def make(self, session_id="sess-1", resume=True, **kw):
        drv = driver.Driver(
            session_id,
            str(self.cwd),
            resume=resume,
            on_event=lambda d, kind, data: self.events.append((kind, data)),
            on_permission=self.on_permission,
            claude=str(self.binary),
            env=self.env(),
            **kw,
        )
        self.drivers.append(drv)
        return drv

    def on_permission(self, drv, request):
        self.permissions.append(request)
        return self.decision

    def logged(self, event=None):
        if not self.log_path.exists():
            return []
        rows = [json.loads(l) for l in self.log_path.read_text().splitlines() if l.strip()]
        return [r for r in rows if event is None or r.get("event") == event]

    def wait_idle(self, drv, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if drv.state == "idle":
                return True
            time.sleep(0.02)
        return False

    def wait_state(self, drv, state, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if drv.state == state:
                return True
            time.sleep(0.02)
        return False


class TestArgv(DriverHarness):
    def test_resume_versus_new(self):
        old = self.make().argv()
        self.assertIn("--resume", old)
        self.assertEqual(old[old.index("--resume") + 1], "sess-1")
        new = self.make(resume=False).argv()
        self.assertIn("--session-id", new)
        self.assertNotIn("--resume", new)

    def test_the_wire_flags_are_always_present(self):
        argv = self.make().argv()
        for flag in ("-p", "--input-format", "--output-format", "--permission-prompt-tool"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "stdio")
        self.assertNotIn("--bare", argv)

    def test_mode_and_model_are_optional_and_mapped(self):
        plain = self.make().argv()
        self.assertNotIn("--permission-mode", plain)
        self.assertNotIn("--model", plain)
        argv = self.make(mode="default", model="default").argv()
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "manual")
        self.assertNotIn("--model", argv)
        argv = self.make(mode="plan", model="haiku").argv()
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")

    def test_a_missing_directory_is_refused_before_spawning(self):
        drv = driver.Driver("s", str(self.tmp / "gone"), resume=True, claude=str(self.binary))
        with self.assertRaises(driver.DriverError):
            drv.start()


class TestLifecycle(DriverHarness):
    def test_start_initialises_and_learns_the_catalogue(self):
        drv = self.make().start()
        self.assertEqual(drv.state, "idle")
        self.assertTrue(drv.alive)
        names = [c["name"] for c in drv.caps.commands]
        self.assertIn("fake-skill", names)
        self.assertEqual(drv.caps.commands[0]["argument_hint"], "<topic>")
        started = self.logged("start")[0]
        self.assertEqual(started["cwd"], str(self.cwd))
        self.assertEqual(started["argv"][started["argv"].index("--resume") + 1], "sess-1")

    def test_the_parents_identity_is_not_inherited(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "parent"
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = "/tmp/x"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_MESSAGING_SOCKET", None)
        from scribe import peer

        drv = driver.Driver("sess-1", str(self.cwd), resume=True, claude=str(self.binary),
                            env=dict(peer.child_env(), FAKE_CLAUDE_LOG=str(self.log_path)))
        self.drivers.append(drv)
        drv.start()
        env = self.logged("start")[0]["env"]
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("CLAUDE_CODE_MESSAGING_SOCKET", env)
        self.assertIn("CLAUDE_CONFIG_DIR", env)

    def test_a_turn_writes_the_transcript_and_ends_idle(self):
        drv = self.make().start()
        reply = drv.send("hello there")
        self.assertEqual(reply, {"ok": True, "queued": False})
        self.assertEqual(drv.state, "running")
        self.assertTrue(self.wait_idle(drv))
        self.assertEqual(drv.turns, 1)
        self.assertEqual(drv.last_result["subtype"], "success")
        kinds = [k for k, _ in self.events]
        self.assertIn("init", kinds)
        self.assertIn("result", kinds)
        self.assertEqual(drv.caps.mode, "default")
        self.assertEqual(drv.caps.skills, ["fake-skill"])
        self.assertEqual(drv.caps.terminal_commands, ["doctor"])
        from scribe import transcript

        path = transcript.find_transcript("sess-1")
        self.assertIsNotNone(path)
        rows = [json.loads(l) for l in open(path)]
        self.assertEqual(rows[0]["type"], "user")
        self.assertEqual(rows[0]["message"]["content"][0]["text"], "hello there")
        self.assertEqual(rows[1]["message"]["content"][0]["text"], "echo: hello there")

    def test_images_travel_as_content_blocks(self):
        drv = self.make().start()
        png = self.tmp / "dot.png"
        png.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"))
        block = driver.image_block(str(png), "image/png")
        self.assertEqual(block["source"]["media_type"], "image/png")
        self.assertIsNone(driver.image_block(str(png), "application/pdf"))
        drv.send("what is this", images=[block])
        self.assertTrue(self.wait_idle(drv))
        self.assertEqual(self.logged("user")[0]["images"], 1)

    def test_a_message_during_a_turn_is_queued_then_sent(self):
        drv = self.make().start()
        drv.send("SLOW first")
        second = drv.send("second")
        self.assertEqual(second, {"ok": True, "queued": True, "depth": 1})
        self.assertEqual(drv.queued(), ["second"])
        self.assertTrue(self.wait_idle(drv, timeout=8))
        self.assertEqual(drv.turns, 2)
        self.assertEqual([r["text"] for r in self.logged("user")], ["SLOW first", "second"])
        self.assertEqual(drv.queued(), [])
        self.assertIn(("turn", {"text": "second", "queued": 0}), self.events)

    def test_a_queued_message_can_be_dropped(self):
        drv = self.make().start()
        drv.send("SLOW first")
        drv.send("second")
        drv.send("third")
        self.assertTrue(drv.drop_queued(0))
        self.assertFalse(drv.drop_queued(5))
        self.assertEqual(drv.queued(), ["third"])
        self.assertTrue(self.wait_idle(drv, timeout=8))
        self.assertEqual([r["text"] for r in self.logged("user")], ["SLOW first", "third"])

    def test_interrupt_ends_the_turn(self):
        drv = self.make().start()
        drv.send("SLOW count")
        time.sleep(0.2)
        self.assertEqual(drv.interrupt(), {"still_queued": []})
        self.assertTrue(self.wait_idle(drv))
        self.assertEqual(drv.last_result["subtype"], "error_during_execution")

    def test_mode_and_model_round_trip(self):
        drv = self.make().start()
        self.assertEqual(drv.set_mode("plan"), "plan")
        self.assertEqual(drv.caps.mode, "plan")
        self.assertIn(("mode", {"mode": "plan"}), self.events)
        with self.assertRaises(driver.DriverError):
            drv.set_mode("nonsense")
        drv.set_model("haiku")
        drv.send("after")
        self.assertTrue(self.wait_idle(drv))
        turn = self.logged("user")[0]
        self.assertEqual((turn["mode"], turn["model"]), ("plan", "haiku"))
        self.assertEqual(drv.as_dict()["mode"], "plan")

    def test_other_requests(self):
        drv = self.make().start()
        self.assertEqual([s["path"] for s in drv.file_suggestions("app")], ["src/app.js"])
        self.assertIn("categories", drv.context_usage())

    def test_stop_closes_stdin_and_the_child_exits(self):
        drv = self.make().start()
        drv.stop()
        self.assertFalse(drv.alive)
        self.assertEqual(drv.state, "exited")
        self.assertEqual(self.logged("eof"), [{"event": "eof"}])
        self.assertIn("exit", [k for k, _ in self.events])
        self.assertIn("error", drv.send("too late"))

    def test_a_child_that_dies_reports_it(self):
        drv = self.make().start()
        drv.send("DIE now")
        self.assertTrue(self.wait_state(drv, "exited"))
        self.assertEqual(drv.exit_code, 3)
        self.assertIn("dying", drv.error)
        exit_event = [d for k, d in self.events if k == "exit"][0]
        self.assertEqual(exit_event["code"], 3)
        with self.assertRaises(driver.DriverError):
            drv.request("interrupt")


class TestPermission(DriverHarness):
    def test_allow_reaches_the_child(self):
        drv = self.make().start()
        drv.send("PERMIT please")
        self.assertTrue(self.wait_idle(drv))
        self.assertEqual(len(self.permissions), 1)
        self.assertEqual(self.permissions[0]["tool_name"], "Bash")
        self.assertEqual(self.permissions[0]["input"]["command"], "touch fake.txt")
        decided = self.logged("permission")[0]["decision"]
        self.assertEqual(decided["behavior"], "allow")
        # The input is echoed back when the host does not rewrite it.
        self.assertEqual(decided["updatedInput"]["command"], "touch fake.txt")

    def test_deny_reaches_the_child(self):
        self.decision = {"behavior": "deny", "message": "not today"}
        drv = self.make().start()
        drv.send("PERMIT please")
        self.assertTrue(self.wait_idle(drv))
        self.assertEqual(self.logged("permission")[0]["decision"], {"behavior": "deny", "message": "not today"})

    def test_a_broken_handler_is_a_deny_not_a_hang(self):
        def boom(drv, request):
            raise RuntimeError("oops")

        drv = driver.Driver("sess-1", str(self.cwd), resume=True, on_permission=boom,
                            claude=str(self.binary), env=self.env())
        self.drivers.append(drv)
        drv.start()
        drv.send("PERMIT please")
        self.assertTrue(self.wait_idle(drv))
        self.assertEqual(self.logged("permission")[0]["decision"]["behavior"], "deny")

    def test_no_handler_is_a_deny(self):
        drv = driver.Driver("sess-1", str(self.cwd), resume=True, claude=str(self.binary), env=self.env())
        self.drivers.append(drv)
        drv.start()
        drv.send("PERMIT please")
        self.assertTrue(self.wait_idle(drv))
        self.assertEqual(self.logged("permission")[0]["decision"]["behavior"], "deny")


class TestBinary(unittest.TestCase):
    def test_the_override_wins(self):
        os.environ["SCRIBE_CLAUDE"] = "/nowhere/claude"
        try:
            self.assertEqual(driver.claude_binary(), "/nowhere/claude")
        finally:
            os.environ.pop("SCRIBE_CLAUDE", None)


if __name__ == "__main__":
    unittest.main()
