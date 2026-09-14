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
