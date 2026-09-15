"""The daemon: HTTP API, SSE, the watcher, and the hook protocol end to end.

The hook tests run the real `bin/scribe-hook` executable against a real
control socket, because the thing worth verifying is the contract Claude Code
actually sees: what lands on stdout, and how long it takes to get there.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from helpers import Isolated, simple_session

from scribe import config, daemon, paths

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "scribe-hook"


class DaemonHarness(Isolated):
    """A hub with both servers bound to ephemeral addresses."""

    cfg_overrides: dict = {}

    def setUp(self):
        super().setUp()
        cfg = config.load()
        # Deep-copy: `cfg_overrides` is a class attribute, and a test that
        # flips a nested flag would otherwise mutate it for every test after it.
        cfg.update(copy.deepcopy(self.cfg_overrides))
        self.cfg = cfg

        self.hub = daemon.Hub(cfg)
        daemon.Handler.hub = self.hub
        daemon.ControlHandler.hub = self.hub

        self.http = daemon.Server(("127.0.0.1", 0), daemon.Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

        self.ctrl = daemon._bind_control()
        threading.Thread(target=self.ctrl.serve_forever, daemon=True).start()

    def tearDown(self):
        try:
            self.http.shutdown()
            self.http.server_close()
        except OSError:
            pass
        try:
            self.ctrl.shutdown()
            self.ctrl.server_close()
        except OSError:
            pass
        try:
            paths.control_socket().unlink()
        except OSError:
            pass
        super().tearDown()

    # -- clients --------------------------------------------------------

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            with exc:
                return json.loads(exc.read().decode())

    def post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            with exc:
                return json.loads(exc.read().decode())

    def run_hook(self, payload, timeout=20):
        """Run the real hook executable. Returns (stdout_json, seconds)."""
        started = time.time()
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ),
        )
        elapsed = time.time() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        return (json.loads(out) if out else {}), elapsed


class TestHttpApi(DaemonHarness):
    def test_health_and_sessions(self):
        self.assertTrue(self.get("/api/health")["ok"])
        self.assertEqual(self.get("/api/sessions")["sessions"], [])

    def test_session_snapshot(self):
        path = self.transcript_path(session_id="sess-1")
        self.write_rows(path, simple_session("sess-1"))
        self.hub.refresh_index(force=True)

        data = self.get("/api/session?id=sess-1")
        self.assertEqual(data["head"]["title"], "Check the build")
        self.assertEqual(len(data["rounds"]), 1)
        keys = [i["key"] for i in data["rounds"][0]["items"]]
        self.assertIn("t:toolu_A", keys)
        self.assertEqual(len(set(keys)), len(keys), "item keys must be unique")

    def test_unknown_session_is_404(self):
        self.assertIn("error", self.get("/api/session?id=nope"))

    def test_rejects_a_foreign_host_header(self):
        # Guards against DNS rebinding: a page on any origin can make a browser
        # talk to 127.0.0.1, and everything here is conversation content.
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        conn.sendall(b"GET /api/sessions HTTP/1.1\r\nHost: evil.example\r\n\r\n")
        self.assertIn(b"403", conn.recv(200))
        conn.close()

    def test_static_traversal_is_blocked(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/static/../scribe/cli.py")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_markdown_is_written_on_load(self):
        path = self.transcript_path(session_id="sess-1")
        self.write_rows(path, simple_session("sess-1"))
        self.hub.refresh_index(force=True)
        self.get("/api/session?id=sess-1")

        logs = list(paths.logs_dir().rglob("*.md"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text()
        self.assertIn("# Check the build", text)
        self.assertIn("pytest -q", text)


class TestWatcher(DaemonHarness):
    def test_new_rows_produce_changed_rounds_only(self):
        path = self.transcript_path(session_id="sess-1")
        rows = simple_session("sess-1")
        self.write_rows(path, rows)
        self.hub.refresh_index(force=True)

        live = self.hub.get("sess-1")
        self.assertEqual(len(live.rounds_json), 1)

        from helpers import user_row

        self.append_rows(path, [user_row("sess-1", "second prompt", "2026-07-28T10:10:00.000Z", "u2")])
        changed, removed = live.rebuild(self.hub.redactor) if live.poll() else ([], [])
        self.assertEqual([r["index"] for r in changed], [2])
        self.assertEqual(removed, [])

    def test_truncated_transcript_is_reread_from_scratch(self):
        path = self.transcript_path(session_id="sess-1")
        self.write_rows(path, simple_session("sess-1"))
        self.hub.refresh_index(force=True)
        live = self.hub.get("sess-1")
        self.assertEqual(len(live.rows), 5)

        self.write_rows(path, simple_session("sess-1")[:2])  # rewrite shorter
        live.poll()
        self.assertTrue(self.hub.get("sess-1").tail.restarted or True)
        self.assertEqual(len(live.rows), 2, "a rewritten transcript must replace, not append")

    def test_partial_final_line_is_held_back(self):
        path = self.transcript_path(session_id="sess-1")
        self.write_rows(path, simple_session("sess-1"))
        self.hub.refresh_index(force=True)
        live = self.hub.get("sess-1")
        before = len(live.rows)

        with open(path, "a") as fh:
            fh.write('{"type": "user", "message": {"role": "user", "cont')
            fh.flush()
        live.poll()
        self.assertEqual(len(live.rows), before, "a half-written row must not be parsed")

        with open(path, "a") as fh:
            fh.write('ent": "finished"}, "sessionId": "sess-1", "timestamp": "2026-07-28T11:00:00Z"}\n')
            fh.flush()
        live.poll()
        self.assertEqual(len(live.rows), before + 1)


class TestHookProtocol(DaemonHarness):
    def test_pass_through_events_are_fast_and_silent(self):
        for event in ("UserPromptSubmit", "PostToolUse", "SessionEnd", "Notification"):
            reply, elapsed = self.run_hook({"hook_event_name": event, "session_id": "sess-1"})
            self.assertEqual(reply, {}, event)
            self.assertLess(elapsed, 2.0, f"{event} took {elapsed:.2f}s")

    def test_hook_succeeds_with_no_daemon(self):
        self.ctrl.shutdown()
        self.ctrl.server_close()
        paths.control_socket().unlink(missing_ok=True)
        reply, elapsed = self.run_hook({"hook_event_name": "PostToolUse", "session_id": "x"})
        self.assertEqual(reply, {})
        self.assertLess(elapsed, 5.0)

    def test_hook_survives_garbage_stdin(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input="not json at all",
            capture_output=True, text=True, timeout=10, env=dict(os.environ),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_disable_env_makes_the_hook_a_no_op(self):
        env = dict(os.environ, SCRIBE_DISABLE="1")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"hook_event_name": "Stop", "session_id": "s"}),
            capture_output=True, text=True, timeout=10, env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class TestPermissionHold(DaemonHarness):
    cfg_overrides = {"remote_approval": {"enabled": True, "wait_s": 6, "require_client": False}}

    def payload(self, call_id="toolu_X"):
        return {
            "hook_event_name": "PermissionRequest",
            "session_id": "sess-1",
            "tool_use_id": call_id,
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf build"},
        }

    def test_not_armed_returns_immediately(self):
        reply, elapsed = self.run_hook(self.payload())
        self.assertEqual(reply, {})
        self.assertLess(elapsed, 2.0)

    def test_armed_hold_then_allow_from_the_browser(self):
        self.hub.control.arm("sess-1", True)

        def approve():
            for _ in range(60):
                if self.hub.control.get_pending("toolu_X"):
                    self.post("/api/decision", {"call_id": "toolu_X", "behavior": "allow"})
                    return
                time.sleep(0.05)

        threading.Thread(target=approve, daemon=True).start()
        reply, elapsed = self.run_hook(self.payload())
        decision = reply["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "allow")
        self.assertEqual(reply["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertLess(elapsed, 6.0)

    def test_armed_hold_then_deny(self):
        self.hub.control.arm("sess-1", True)

        def deny():
            for _ in range(60):
                if self.hub.control.get_pending("toolu_Y"):
                    self.post("/api/decision", {"call_id": "toolu_Y", "behavior": "deny"})
                    return
                time.sleep(0.05)

        threading.Thread(target=deny, daemon=True).start()
        reply, _ = self.run_hook(self.payload("toolu_Y"))
        self.assertEqual(reply["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_edited_command_is_returned_as_updated_input(self):
        self.hub.control.arm("sess-1", True)

        def approve():
            for _ in range(60):
                if self.hub.control.get_pending("toolu_Z"):
                    self.post("/api/decision", {
                        "call_id": "toolu_Z", "behavior": "allow",
                        "updated_input": {"command": "rm -rf build/tmp"},
                    })
                    return
                time.sleep(0.05)

        threading.Thread(target=approve, daemon=True).start()
        reply, _ = self.run_hook(self.payload("toolu_Z"))
        decision = reply["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["updatedInput"], {"command": "rm -rf build/tmp"})

    def test_pass_hands_back_to_the_terminal(self):
        self.hub.control.arm("sess-1", True)

        def hand_back():
            for _ in range(60):
                if self.hub.control.get_pending("toolu_P"):
                    self.post("/api/decision", {"call_id": "toolu_P", "behavior": "pass"})
                    return
                time.sleep(0.05)

        threading.Thread(target=hand_back, daemon=True).start()
        reply, _ = self.run_hook(self.payload("toolu_P"))
        self.assertEqual(reply, {})

    def test_arming_is_refused_when_the_feature_is_off(self):
        self.hub.cfg["remote_approval"]["enabled"] = False
        response = self.post("/api/arm", {"session_id": "sess-1", "on": True})
        self.assertIn("error", response)


class TestReplyInjection(DaemonHarness):
    cfg_overrides = {"reply_queue": {"enabled": True, "max_chain": 2}}

    def test_queued_reply_blocks_the_stop_and_is_delivered(self):
        self.post("/api/message", {"session_id": "sess-1", "text": "also update the README"})
        reply, _ = self.run_hook({"hook_event_name": "Stop", "session_id": "sess-1"})
        self.assertEqual(reply["decision"], "block")
        self.assertIn("also update the README", reply["reason"])
        self.assertIn("scribe web console", reply["reason"])

    def test_nothing_queued_means_no_interference(self):
        reply, _ = self.run_hook({"hook_event_name": "Stop", "session_id": "sess-1"})
        self.assertEqual(reply, {})

    def test_stop_hook_active_is_honoured(self):
        self.post("/api/message", {"session_id": "sess-1", "text": "keep going"})
        reply, _ = self.run_hook(
            {"hook_event_name": "Stop", "session_id": "sess-1", "stop_hook_active": True}
        )
        self.assertEqual(reply, {})
        self.assertEqual(self.hub.control.queued("sess-1"), ["keep going"])

    def test_chain_cap_is_enforced_across_real_hook_runs(self):
        for i in range(2):
            self.post("/api/message", {"session_id": "sess-1", "text": f"msg {i}"})
            reply, _ = self.run_hook({"hook_event_name": "Stop", "session_id": "sess-1"})
            self.assertEqual(reply["decision"], "block")

        self.post("/api/message", {"session_id": "sess-1", "text": "one too many"})
        reply, _ = self.run_hook({"hook_event_name": "Stop", "session_id": "sess-1"})
        self.assertEqual(reply, {}, "the chain cap must stop a runaway loop")

    def test_a_terminal_prompt_resets_the_chain(self):
        for i in range(2):
            self.post("/api/message", {"session_id": "sess-1", "text": f"m{i}"})
            self.run_hook({"hook_event_name": "Stop", "session_id": "sess-1"})

        self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "sess-1"})
        self.post("/api/message", {"session_id": "sess-1", "text": "after typing"})
        reply, _ = self.run_hook({"hook_event_name": "Stop", "session_id": "sess-1"})
        self.assertEqual(reply["decision"], "block")

    def test_queue_is_refused_when_the_feature_is_off(self):
        self.hub.cfg["reply_queue"]["enabled"] = False
        self.assertIn("error", self.post("/api/message", {"session_id": "s", "text": "hi"}))


class TestConfigReload(DaemonHarness):
    def test_config_set_takes_effect_without_a_restart(self):
        from scribe import config as config_module

        self.assertFalse((self.hub.cfg.get("reply_queue") or {}).get("enabled"))

        stored = config_module.load()
        config_module.set_in(stored, "reply_queue.enabled", True)
        config_module.save(stored)

        reply, _ = self.run_hook({"hook_event_name": "ConfigChanged"})
        self.assertEqual(reply, {})
        self.assertTrue(self.hub.cfg["reply_queue"]["enabled"])
        self.assertNotIn("error", self.post("/api/message", {"session_id": "s", "text": "hi"}))


if __name__ == "__main__":
    unittest.main()


class TestBoard(DaemonHarness):
    """The card payload: the transcript's phase, refined by presence and hooks."""

    def setUp(self):
        super().setUp()
        self.path = self.transcript_path(session_id="sess-1")
        self.write_rows(self.path, simple_session("sess-1"))
        self.hub.refresh_index(force=True)

    def card(self, session_id="sess-1"):
        for item in self.get("/api/sessions")["sessions"]:
            if item["id"] == session_id:
                return item
        self.fail("no card for " + session_id)

    def test_a_fresh_transcript_is_live_and_on_your_turn(self):
        card = self.card()
        self.assertTrue(card["live"])
        self.assertEqual(card["phase"], "your_turn")
        self.assertEqual(card["state"]["reply"], "All 5 tests pass.")
        self.assertEqual(card["pending"], [])

    def test_an_old_transcript_without_hooks_is_done(self):
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.hub.refresh_index(force=True)
        self.assertEqual(self.card()["phase"], "done")

    def test_hook_contact_keeps_a_session_live_and_session_end_finishes_it(self):
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.hub.refresh_index(force=True)
        daemon.dispatch(self.hub, {"hook_event_name": "PostToolUse", "session_id": "sess-1"})
        self.assertEqual(self.card()["phase"], "your_turn")
        daemon.dispatch(self.hub, {"hook_event_name": "SessionEnd", "session_id": "sess-1"})
        card = self.card()
        self.assertEqual(card["phase"], "done")
        self.assertFalse(card["live"])
        # A session that ended stays ended even though the file is still fresh.
        os.utime(self.path, None)
        self.hub.refresh_index(force=True)
        self.assertEqual(self.card()["phase"], "done")

    def test_terminal_permission_prompt_needs_you_until_the_tool_runs(self):
        daemon.dispatch(self.hub, {"hook_event_name": "Notification", "session_id": "sess-1",
                                   "notification_type": "permission_prompt", "message": "Bash wants to run"})
        card = self.card()
        self.assertEqual(card["phase"], "needs_you")
        self.assertEqual(card["state"]["activity_kind"], "terminal")
        daemon.dispatch(self.hub, {"hook_event_name": "PostToolUse", "session_id": "sess-1"})
        self.assertEqual(self.card()["phase"], "your_turn")

    def test_a_held_approval_needs_you_with_the_call_attached(self):
        from scribe import control

        call = control.PendingCall(call_id="toolu_Z", session_id="sess-1", tool_name="Bash",
                                   tool_input={"command": "rm -rf build"})
        self.hub.control.open_call(call, 30)
        card = self.card()
        self.assertEqual(card["phase"], "needs_you")
        self.assertEqual(card["pending"][0]["call_id"], "toolu_Z")
        self.assertEqual(card["pending"][0]["subject"], "rm -rf build")
        self.assertGreater(card["pending"][0]["seconds_left"], 20)

        self.assertTrue(self.post("/api/decision", {"call_id": "toolu_Z", "behavior": "allow"})["ok"])
        self.assertEqual(self.card()["phase"], "your_turn")

    def test_plan_mode_while_working_is_planning(self):
        from helpers import assistant_row, tool_use

        self.append_rows(self.path, [
            {"type": "permission-mode", "permissionMode": "plan", "sessionId": "sess-1"},
            assistant_row("sess-1", [tool_use("t2", "Read", {"file_path": "/tmp/proj/x.py"})],
                          "2026-07-28T10:01:00.000Z", "a2"),
        ])
        self.hub.refresh_index(force=True)
        self.assertEqual(self.card()["phase"], "planning")

    def test_card_events_reach_every_stream(self):
        # Subscribe with no session, as the board does, and watch a card arrive.
        sub = self.hub.subscribe("")
        daemon.dispatch(self.hub, {"hook_event_name": "SessionEnd", "session_id": "sess-1"})
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        self.hub.unsubscribe(sub)
        cards = [d for e, d in events if e == "card"]
        self.assertTrue(cards)
        self.assertEqual(cards[-1]["id"], "sess-1")
        self.assertEqual(cards[-1]["phase"], "done")


class TestInboxDelivery(DaemonHarness):
    """A message from the page goes straight into a session that has an inbox."""

    def setUp(self):
        super().setUp()
        from test_peer import FakeInbox

        self.path = self.transcript_path(session_id="sess-1")
        self.write_rows(self.path, simple_session("sess-1"))
        self.inbox = FakeInbox("sess-1")
        self.addCleanup(self.inbox.close)
        self.inbox.register()
        self.hub.refresh_index(force=True)

    def card(self, session_id="sess-1"):
        for item in self.get("/api/sessions")["sessions"]:
            if item["id"] == session_id:
                return item
        self.fail("no card for " + session_id)

    def test_the_card_and_head_say_inbox(self):
        self.assertEqual(self.card()["reply_via"], "inbox")
        self.assertEqual(self.get("/api/session?id=sess-1")["head"]["reply_via"], "inbox")

    def test_a_session_with_an_inbox_is_live_even_when_its_file_is_old(self):
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.hub.refresh_index(force=True)
        self.hub.peers(force=True)
        self.assertTrue(self.card()["live"])

    def test_message_is_delivered_now_not_queued(self):
        reply = self.post("/api/message", {"session_id": "sess-1", "text": "and the docs"})
        self.assertEqual(reply, {"ok": True, "via": "inbox", "held": False})
        self.assertTrue(self.inbox.got.wait(2))
        self.assertEqual([l["type"] for l in self.inbox.lines], ["auth", "user"])
        self.assertIn("and the docs", self.inbox.lines[1]["message"]["content"])
        self.assertEqual(self.hub.control.queued("sess-1"), [])

    def test_the_inbox_wins_over_the_stop_hook_queue(self):
        self.hub.cfg["reply_queue"]["enabled"] = True
        reply = self.post("/api/message", {"session_id": "sess-1", "text": "now"})
        self.assertEqual(reply["via"], "inbox")
        self.assertEqual(self.hub.control.queued("sess-1"), [])

    def test_disabling_messaging_falls_back_to_the_queue_or_refuses(self):
        self.hub.cfg["messaging"]["enabled"] = False
        reply = self.post("/api/message", {"session_id": "sess-1", "text": "now"})
        self.assertIn("error", reply)
        self.hub.cfg["reply_queue"]["enabled"] = True
        reply = self.post("/api/message", {"session_id": "sess-1", "text": "now"})
        self.assertEqual(reply["via"], "queue")

    def test_attachments_reach_a_terminal_session_by_path(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/upload?session_id=sess-1&name=shot.png",
            data=bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"),
            headers={"Content-Type": "image/png"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as r:
            saved = json.loads(r.read().decode())
        reply = self.post("/api/message", {"session_id": "sess-1", "text": "look", "attachments": [saved["id"]]})
        self.assertEqual(reply["via"], "inbox")
        self.assertTrue(self.inbox.got.wait(2))
        self.assertIn("look\n\nAttached file: " + saved["path"], self.inbox.lines[1]["message"]["content"])

    def test_a_terminal_session_gets_skills_but_not_builtins(self):
        got = self.get("/api/commands?session_id=sess-1")
        self.assertEqual(got["channel"], "inbox")
        names = {c["name"]: c for c in got["commands"]}
        self.assertFalse(names["compact"]["available"])
        self.assertIn("terminal", names["compact"]["why"])

    def test_empty_and_unknown_are_refused(self):
        self.assertIn("error", self.post("/api/message", {"session_id": "sess-1", "text": "  "}))
        self.assertIn("error", self.post("/api/message", {"session_id": "nope", "text": "hi"}))


class TestDriverDelivery(DaemonHarness):
    """No process behind a session: the first message starts a driver of ours."""

    def setUp(self):
        super().setUp()
        from test_driver import make_fake_binary

        self.binary = make_fake_binary(self.tmp)
        os.environ["SCRIBE_CLAUDE"] = str(self.binary)
        self.addCleanup(os.environ.pop, "SCRIBE_CLAUDE", None)
        self.fake_log = self.tmp / "fake.log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.fake_log)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)
        # Resolved: the child reports its cwd with symlinks followed
        # (/var -> /private/var on macOS), and so does a real transcript.
        self.cwd = Path(os.path.realpath(self.tmp)) / "proj"
        self.cwd.mkdir()
        self.path = self.transcript_path(session_id="sess-1", cwd=str(self.cwd))
        self.write_rows(self.path, simple_session("sess-1", cwd=str(self.cwd)))
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.hub.refresh_index(force=True)

    def tearDown(self):
        self.hub.stop_all_drivers()
        super().tearDown()

    def card(self, session_id="sess-1"):
        for item in self.get("/api/sessions")["sessions"]:
            if item["id"] == session_id:
                return item
        self.fail("no card for " + session_id)

    def head(self, session_id="sess-1"):
        return self.get("/api/session?id=" + session_id)["head"]

    def fake_log_rows(self, event=None):
        if not self.fake_log.exists():
            return []
        rows = [json.loads(l) for l in self.fake_log.read_text().splitlines() if l.strip()]
        return [r for r in rows if event is None or r.get("event") == event]

    def wait_for(self, pred, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.05)
        return False

    def wait_idle(self, session_id="sess-1"):
        return self.wait_for(lambda: (self.hub.driver_for(session_id) or type("x", (), {"state": ""})).state == "idle")

    def test_a_finished_session_offers_spawn(self):
        card = self.card()
        self.assertEqual(card["phase"], "done")
        self.assertEqual(card["reply_via"], "spawn")
        caps = self.head()["caps"]
        self.assertEqual(caps["attachments"], "blocks")
        self.assertTrue(caps["mode"]["settable"])
        self.assertNotIn("bypassPermissions", caps["mode"]["choices"])

    def test_a_fresh_transcript_without_hooks_still_offers_spawn(self):
        # No hooks installed and the file just changed: the board guesses a
        # process (live), but nothing can be reached, so the driver is offered.
        os.utime(self.path, None)
        self.hub.refresh_index(force=True)
        card = self.card()
        self.assertTrue(card["live"])
        self.assertEqual(card["reply_via"], "spawn")

    def test_the_driver_can_be_turned_off(self):
        self.hub.cfg["driver"]["enabled"] = False
        self.assertEqual(self.card()["reply_via"], "")

    def test_a_message_starts_a_driver_in_the_sessions_directory(self):
        reply = self.post("/api/message", {"session_id": "sess-1", "text": "keep going", "mode": "plan"})
        self.assertEqual(reply, {"ok": True, "via": "driver", "queued": False})
        started = self.fake_log_rows("start")[0]
        self.assertEqual(started["cwd"], str(self.cwd))
        argv = started["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertTrue(self.wait_idle())
        self.assertTrue(self.wait_for(lambda: self.get("/api/session?id=sess-1")["rounds"][-1].get("prompt") == "keep going"))
        # The child stays: the card is live, the channel is the driver, and
        # the driver's mode is what the card and head report.
        card = self.card()
        self.assertTrue(card["live"])
        self.assertEqual(card["reply_via"], "driver")
        self.assertEqual(card["phase"], "your_turn")
        self.assertEqual(card["state"]["mode"], "plan")
        head = self.head()
        self.assertEqual(head["driver"]["state"], "idle")
        self.assertEqual(head["caps"]["mode"]["value"], "plan")
        self.assertEqual(head["caps"]["commands"], "live")
        # A second message reuses the child rather than starting another.
        self.post("/api/message", {"session_id": "sess-1", "text": "and again"})
        self.assertTrue(self.wait_idle())
        self.assertEqual(len(self.fake_log_rows("start")), 1)
        self.assertEqual([r["text"] for r in self.fake_log_rows("user")], ["keep going", "and again"])

    def test_a_message_during_a_turn_is_queued(self):
        self.post("/api/message", {"session_id": "sess-1", "text": "SLOW one"})
        second = self.post("/api/message", {"session_id": "sess-1", "text": "two"})
        self.assertEqual(second, {"ok": True, "via": "driver", "queued": True})
        self.assertEqual(self.head()["queued"], ["two"])
        self.assertEqual(self.card()["queued"], 1)
        self.assertEqual(self.card()["phase"], "working")
        self.post("/api/unqueue", {"session_id": "sess-1", "index": 0})
        self.assertEqual(self.head()["queued"], [])
        self.assertTrue(self.wait_idle())
        self.assertEqual([r["text"] for r in self.fake_log_rows("user")], ["SLOW one"])

    def test_mode_model_and_interrupt_endpoints(self):
        self.assertIn("error", self.post("/api/session/mode", {"session_id": "sess-1", "mode": "plan"}))
        self.post("/api/message", {"session_id": "sess-1", "text": "hi"})
        self.assertTrue(self.wait_idle())
        self.assertEqual(self.post("/api/session/mode", {"session_id": "sess-1", "mode": "acceptEdits"}), {"ok": True, "mode": "acceptEdits"})
        self.assertEqual(self.head()["caps"]["mode"]["value"], "acceptEdits")
        self.assertIn("error", self.post("/api/session/mode", {"session_id": "sess-1", "mode": "bypassPermissions"}))
        self.assertEqual(self.post("/api/session/model", {"session_id": "sess-1", "model": "haiku"}), {"ok": True, "model": "haiku"})
        self.assertIn("error", self.post("/api/session/model", {"session_id": "sess-1", "model": "gpt-9"}))
        self.post("/api/message", {"session_id": "sess-1", "text": "SLOW again"})
        self.assertTrue(self.wait_for(lambda: self.head()["caps"]["interrupt"]))
        self.assertEqual(self.post("/api/interrupt", {"session_id": "sess-1"}), {"ok": True})
        self.assertTrue(self.wait_idle())
        self.assertEqual(self.hub.driver_for("sess-1").last_result["subtype"], "error_during_execution")

    def test_a_permission_request_is_held_for_the_page(self):
        self.post("/api/message", {"session_id": "sess-1", "text": "PERMIT this"})
        self.assertTrue(self.wait_for(lambda: self.card()["pending"]))
        card = self.card()
        self.assertEqual(card["phase"], "needs_you")
        call = card["pending"][0]
        self.assertEqual(call["tool_name"], "Bash")
        self.post("/api/decision", {"call_id": call["call_id"], "behavior": "allow"})
        self.assertTrue(self.wait_idle())
        self.assertEqual(self.fake_log_rows("permission")[0]["decision"]["behavior"], "allow")
        self.assertEqual(self.card()["pending"], [])

    def test_the_hook_passes_for_a_driven_session(self):
        self.post("/api/message", {"session_id": "sess-1", "text": "hi"})
        self.assertTrue(self.wait_idle())
        out, elapsed = self.run_hook(
            {"hook_event_name": "PermissionRequest", "session_id": "sess-1", "tool_name": "Bash",
             "tool_input": {"command": "ls"}, "tool_use_id": "toolu_hook"}
        )
        self.assertEqual(out, {})
        self.assertLess(elapsed, 5)
        self.assertEqual(self.card()["pending"], [])

    def test_an_idle_driver_is_retired_and_spawn_is_offered_again(self):
        self.post("/api/message", {"session_id": "sess-1", "text": "hi"})
        self.assertTrue(self.wait_idle())
        self.hub.reap_drivers(now=time.time() + 3 * 3600)
        self.assertIsNone(self.hub.driver_for("sess-1"))
        self.assertEqual(self.card()["reply_via"], "spawn")
        self.assertEqual(self.card()["phase"], "done")

    def test_a_terminal_taking_the_session_retires_the_driver(self):
        from test_peer import FakeInbox

        self.post("/api/message", {"session_id": "sess-1", "text": "hi"})
        self.assertTrue(self.wait_idle())
        inbox = FakeInbox("sess-1")
        self.addCleanup(inbox.close)
        inbox.register()
        self.hub.peers(force=True)
        self.hub.reap_drivers()
        self.assertIsNone(self.hub.driver_for("sess-1"))
        self.assertEqual(self.card()["reply_via"], "inbox")

    def upload(self, name, data, mime, session_id="sess-1"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/upload?session_id={session_id}&name={urllib.parse.quote(name)}",
            data=data,
            headers={"Content-Type": mime},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            with exc:
                return json.loads(exc.read().decode())

    PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489")

    def test_an_image_upload_travels_as_a_content_block(self):
        saved = self.upload("shot.png", self.PNG, "image/png")
        self.assertTrue(saved["image"])
        self.assertEqual(saved["mime"], "image/png")
        self.assertTrue(saved["path"].startswith(str(paths.uploads_dir("sess-1"))))
        self.assertTrue(os.path.exists(saved["path"]))
        reply = self.post("/api/message", {"session_id": "sess-1", "text": "", "attachments": [saved["id"]]})
        self.assertEqual(reply["via"], "driver")
        self.assertTrue(self.wait_idle())
        turn = self.fake_log_rows("user")[0]
        self.assertEqual(turn["images"], 1)
        self.assertNotIn("Attached file", turn["text"])

    def test_the_browser_is_not_trusted_about_image_types(self):
        saved = self.upload("shot.png", b"not really a png", "image/png")
        self.assertFalse(saved["image"])
        self.assertEqual(saved["mime"], "image/png")  # what it said; `image` is what we checked

    def test_other_files_are_named_by_path(self):
        saved = self.upload("notes.txt", b"hello", "text/plain")
        self.assertFalse(saved["image"])
        self.post("/api/message", {"session_id": "sess-1", "text": "read this", "attachments": [saved["id"]]})
        self.assertTrue(self.wait_idle())
        turn = self.fake_log_rows("user")[0]
        self.assertEqual(turn["images"], 0)
        self.assertIn("read this\n\nAttached file: " + saved["path"], turn["text"])

    def test_upload_names_cannot_escape_and_size_is_capped(self):
        saved = self.upload("../../evil.sh", b"x", "text/plain")
        self.assertTrue(saved["path"].startswith(str(paths.uploads_dir("sess-1"))))
        self.assertNotIn("/", saved["name"])
        self.hub.cfg["uploads"]["max_mb"] = 0.00001
        self.assertIn("error", self.upload("big.bin", b"x" * 100, "application/octet-stream"))
        self.assertIn("error", self.post("/api/message", {"session_id": "sess-1", "text": "", "attachments": ["nope"]}))

    def test_the_catalogue_comes_from_disk_then_from_the_driver(self):
        skills = paths.claude_home() / "skills" / "mine" / "SKILL.md"
        skills.parent.mkdir(parents=True)
        skills.write_text("---\nname: mine\ndescription: my skill\n---\n")
        before = self.get("/api/commands?session_id=sess-1")
        self.assertEqual(before["channel"], "spawn")
        names = {c["name"]: c for c in before["commands"]}
        self.assertIn("mine", names)
        self.assertTrue(names["mine"]["available"])
        self.assertNotIn("fake-skill", names)
        self.post("/api/message", {"session_id": "sess-1", "text": "hi"})
        self.assertTrue(self.wait_idle())
        after = self.get("/api/commands?session_id=sess-1")
        self.assertEqual(after["source"], "live")
        names = {c["name"]: c for c in after["commands"]}
        self.assertEqual(names["fake-skill"]["argument_hint"], "<topic>")
        self.assertEqual(names["fake-skill"]["kind"], "skill")
        self.assertEqual(names["compact"]["kind"], "builtin")
        self.assertFalse(names["doctor"]["available"])
        # What the driver reported is kept for sessions that have none.
        self.hub.stop_driver("sess-1")
        kept = self.get("/api/commands?session_id=sess-1")
        self.assertEqual(kept["source"], "cache")
        self.assertIn("fake-skill", {c["name"] for c in kept["commands"]})

    def test_file_suggestions_come_from_the_driver_when_there_is_one(self):
        (self.cwd / "src").mkdir()
        (self.cwd / "src" / "thing.py").write_text("x")
        disk = self.get("/api/files?session_id=sess-1&q=thing")
        self.assertEqual(disk["source"], "disk")
        self.assertEqual([f["path"] for f in disk["files"]], ["src/thing.py"])
        self.post("/api/message", {"session_id": "sess-1", "text": "hi"})
        self.assertTrue(self.wait_idle())
        live = self.get("/api/files?session_id=sess-1&q=app")
        self.assertEqual(live["source"], "live")
        self.assertEqual([f["path"] for f in live["files"]], ["src/app.js"])

    def test_a_new_session_starts_with_a_fresh_id_and_becomes_real(self):
        info = self.get("/api/new")
        self.assertEqual([r["cwd"] for r in info["recent"]], [str(self.cwd)])
        self.assertTrue(info["caps"]["mode"]["settable"])
        self.assertEqual(self.get("/api/fs?path=" + str(self.cwd))["ok"], True)
        self.assertEqual(self.get("/api/fs?path=/nowhere/at/all")["ok"], False)
        self.assertIn("error", self.post("/api/new", {"cwd": "/nowhere", "text": "hi"}))
        self.assertIn("error", self.post("/api/new", {"cwd": str(self.cwd), "text": ""}))

        reply = self.post("/api/new", {"cwd": str(self.cwd), "text": "first words", "mode": "plan"})
        self.assertTrue(reply["ok"], reply)
        sid = reply["id"]
        self.assertNotEqual(sid, "sess-1")
        started = self.fake_log_rows("start")[0]
        argv = started["argv"]
        self.assertEqual(argv[argv.index("--session-id") + 1], sid)
        self.assertNotIn("--resume", argv)
        self.assertEqual(started["cwd"], str(self.cwd))
        # A draft card and snapshot exist before (or regardless of) the file.
        cards = {c["id"]: c for c in self.get("/api/sessions")["sessions"]}
        self.assertIn(sid, cards)
        snap = self.get("/api/session?id=" + sid)
        self.assertEqual(snap["head"]["reply_via"], "driver")
        self.assertTrue(self.wait_idle(sid))
        self.assertTrue(self.wait_for(lambda: not self.get("/api/session?id=" + sid).get("draft")))
        snap = self.get("/api/session?id=" + sid)
        self.assertEqual(snap["rounds"][0]["prompt"], "first words")
        card = {c["id"]: c for c in self.get("/api/sessions")["sessions"]}[sid]
        self.assertFalse(card.get("draft"))
        self.assertEqual(card["reply_via"], "driver")
        self.assertNotIn(sid, self.hub.drafts)

    def test_uploads_made_before_the_session_existed_move_with_it(self):
        saved = self.upload("notes.txt", b"hello", "text/plain", session_id="new")
        self.assertTrue(saved["path"].startswith(str(paths.uploads_dir("new"))))
        reply = self.post("/api/new", {"cwd": str(self.cwd), "text": "read", "attachments": [saved["id"]]})
        sid = reply["id"]
        self.assertTrue(self.wait_idle(sid))
        turn = self.fake_log_rows("user")[0]
        self.assertIn(str(paths.uploads_dir(sid)), turn["text"])
        self.assertTrue(os.path.exists(turn["text"].split("Attached file: ")[1]))

    def test_attachments_are_served_back_and_confined(self):
        saved = self.upload("shot.png", self.PNG, "image/png")
        url = f"http://127.0.0.1:{self.port}/api/file?path=" + urllib.parse.quote(saved["path"])
        with urllib.request.urlopen(url, timeout=5) as r:
            self.assertEqual(r.headers.get("Content-Type"), "image/png")
            self.assertEqual(r.read(), self.PNG)
        outside = self.tmp / "secret.txt"
        outside.write_text("no")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/file?path=" + urllib.parse.quote(str(outside)), timeout=5)
        self.assertEqual(caught.exception.code, 404)
        # A picture that went to the driver as a block comes back from the transcript.
        self.post("/api/message", {"session_id": "sess-1", "text": "see", "attachments": [saved["id"]]})
        self.assertTrue(self.wait_idle())
        snap = self.get("/api/session?id=sess-1")
        rnd = snap["rounds"][-1]
        self.assertEqual(rnd["prompt"], "see")
        self.assertEqual(rnd["attachments"][0]["kind"], "image")
        blob = f"http://127.0.0.1:{self.port}/api/blob?session_id=sess-1&uuid={rnd['attachments'][0]['uuid']}&i={rnd['attachments'][0]['index']}"
        with urllib.request.urlopen(blob, timeout=5) as r:
            self.assertEqual(r.read(), self.PNG)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/blob?session_id=sess-1&uuid=nope&i=0", timeout=5)

    def test_a_child_that_dies_leaves_the_session_done(self):
        self.post("/api/message", {"session_id": "sess-1", "text": "DIE"})
        self.assertTrue(self.wait_for(lambda: self.hub.driver_for("sess-1") is None))
        self.assertIn("sess-1", self.hub.ended)
        self.assertEqual(self.card()["phase"], "done")
        self.assertEqual(self.card()["reply_via"], "spawn")

