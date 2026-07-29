"""Two-way control: approval holds and the reply queue.

These guards are the ones with real consequences. A stuck approval hold means a
terminal that looks hung; a missing loop guard means a conversation that will
not stop. Both are tested against the state machine directly, without sockets.
"""

from __future__ import annotations

import threading
import time
import unittest

from helpers import Isolated

from scribe import control


class TestApprovalHold(Isolated):
    def setUp(self):
        super().setUp()
        self.state = control.ControlState()

    def pending(self, call_id="c1", session="s1"):
        return control.PendingCall(
            call_id=call_id, session_id=session, tool_name="Bash",
            tool_input={"command": "rm -rf build"},
        )

    def test_allow_from_the_browser(self):
        call = self.state.open_call(self.pending(), wait_s=5)
        threading.Timer(0.05, lambda: self.state.resolve("c1", "allow")).start()
        behavior, updated = self.state.wait_for(call, 5, still_watching=lambda: True)
        self.assertEqual(behavior, "allow")
        self.assertIsNone(updated)

    def test_deny_from_the_browser(self):
        call = self.state.open_call(self.pending(), wait_s=5)
        threading.Timer(0.05, lambda: self.state.resolve("c1", "deny")).start()
        behavior, _ = self.state.wait_for(call, 5, still_watching=lambda: True)
        self.assertEqual(behavior, "deny")

    def test_allow_with_an_edited_command(self):
        call = self.state.open_call(self.pending(), wait_s=5)
        threading.Timer(
            0.05, lambda: self.state.resolve("c1", "allow", {"command": "rm -rf build/tmp"})
        ).start()
        behavior, updated = self.state.wait_for(call, 5, still_watching=lambda: True)
        self.assertEqual(behavior, "allow")
        self.assertEqual(updated, {"command": "rm -rf build/tmp"})

    def test_timeout_falls_through_to_the_terminal(self):
        call = self.state.open_call(self.pending(), wait_s=0.3)
        started = time.time()
        behavior, _ = self.state.wait_for(call, 0.3, still_watching=lambda: True)
        self.assertEqual(behavior, "pass")
        self.assertLess(time.time() - started, 2.0)

    def test_losing_the_last_viewer_releases_immediately(self):
        # Closing the tab must give the terminal back at once, not `wait_s`
        # later — otherwise the session looks hung for two minutes.
        call = self.state.open_call(self.pending(), wait_s=30)
        watching = [True]
        threading.Timer(0.05, lambda: watching.__setitem__(0, False)).start()
        started = time.time()
        behavior, _ = self.state.wait_for(call, 30, still_watching=lambda: watching[0])
        self.assertEqual(behavior, "pass")
        self.assertLess(time.time() - started, 2.0)

    def test_disarming_releases_every_waiting_call(self):
        call = self.state.open_call(self.pending(), wait_s=30)
        self.state.arm("s1", True)
        threading.Timer(0.05, lambda: self.state.arm("s1", False)).start()
        started = time.time()
        behavior, _ = self.state.wait_for(call, 30, still_watching=lambda: True)
        self.assertEqual(behavior, "pass")
        self.assertLess(time.time() - started, 2.0)

    def test_a_decided_call_cannot_be_decided_again(self):
        call = self.state.open_call(self.pending(), wait_s=5)
        self.assertTrue(self.state.resolve("c1", "allow"))
        self.assertFalse(self.state.resolve("c1", "deny"))
        behavior, _ = self.state.wait_for(call, 5, still_watching=lambda: True)
        self.assertEqual(behavior, "allow")

    def test_session_end_releases_pending_calls(self):
        call = self.state.open_call(self.pending(), wait_s=30)
        threading.Timer(0.05, lambda: self.state.forget("s1")).start()
        behavior, _ = self.state.wait_for(call, 30, still_watching=lambda: True)
        self.assertEqual(behavior, "pass")

    def test_explanation_can_arrive_after_the_card_is_shown(self):
        self.state.open_call(self.pending(), wait_s=5)
        updated = self.state.annotate("c1", "Deletes the build directory.", 1)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.explanation, "Deletes the build directory.")
        self.assertEqual(updated.explanation_tier, 1)


class TestReplyQueue(Isolated):
    def setUp(self):
        super().setUp()
        self.state = control.ControlState()

    def test_queue_and_take(self):
        self.state.enqueue("s1", "try the other branch")
        text = self.state.take_queued("s1", max_chain=5, stop_hook_active=False)
        self.assertEqual(text, "try the other branch")
        self.assertEqual(self.state.queued("s1"), [])

    def test_multiple_messages_are_joined(self):
        self.state.enqueue("s1", "first")
        self.state.enqueue("s1", "second")
        self.assertEqual(
            self.state.take_queued("s1", 5, False), "first\n\nsecond"
        )

    def test_empty_messages_are_not_queued(self):
        self.assertEqual(self.state.enqueue("s1", "   "), 0)
        self.assertEqual(self.state.queued("s1"), [])

    def test_stop_hook_active_never_injects(self):
        # This is the loop guard: Claude Code sets stop_hook_active when the
        # stop it is reporting was itself caused by a hook block. Injecting
        # again there is how you build a conversation that will not end.
        self.state.enqueue("s1", "keep going")
        self.assertEqual(self.state.take_queued("s1", 5, stop_hook_active=True), "")
        self.assertEqual(self.state.queued("s1"), ["keep going"])

    def test_chain_cap_stops_runaway_injection(self):
        for i in range(10):
            self.state.enqueue("s1", f"msg {i}")
            self.state.take_queued("s1", max_chain=3, stop_hook_active=False)
        self.assertEqual(self.state.chain_count("s1"), 3)
        self.state.enqueue("s1", "one more")
        self.assertEqual(self.state.take_queued("s1", 3, False), "")

    def test_a_real_prompt_resets_the_chain(self):
        for i in range(3):
            self.state.enqueue("s1", f"m{i}")
            self.state.take_queued("s1", 3, False)
        self.state.reset_chain("s1")  # what UserPromptSubmit does
        self.state.enqueue("s1", "after typing in the terminal")
        self.assertEqual(self.state.take_queued("s1", 3, False), "after typing in the terminal")

    def test_dropping_a_queued_message(self):
        self.state.enqueue("s1", "a")
        self.state.enqueue("s1", "b")
        self.assertTrue(self.state.drop_queued("s1", 0))
        self.assertEqual(self.state.queued("s1"), ["b"])
        self.assertFalse(self.state.drop_queued("s1", 9))

    def test_queues_are_per_session(self):
        self.state.enqueue("s1", "for one")
        self.state.enqueue("s2", "for two")
        self.assertEqual(self.state.take_queued("s1", 5, False), "for one")
        self.assertEqual(self.state.queued("s2"), ["for two"])

    def test_injection_is_labelled_as_coming_from_the_web(self):
        text = control.format_injection("do the thing")
        self.assertIn("scribe web console", text)
        self.assertTrue(text.endswith("do the thing"))


class TestArming(Isolated):
    def test_arm_is_per_session_and_defaults_off(self):
        state = control.ControlState()
        self.assertFalse(state.is_armed("s1"))
        state.arm("s1", True)
        self.assertTrue(state.is_armed("s1"))
        self.assertFalse(state.is_armed("s2"))
        state.arm("s1", False)
        self.assertFalse(state.is_armed("s1"))


if __name__ == "__main__":
    unittest.main()
