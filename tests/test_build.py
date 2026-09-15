"""The builder: transcript rows -> session model."""

from __future__ import annotations

import json
import unittest

from helpers import (
    Isolated,
    assistant_row,
    base,
    simple_session,
    tool_result_row,
    tool_use,
    user_row,
)

from scribe import build
from scribe.model import Notice, Text, Thinking, ToolCall


class TestRounds(Isolated):
    def test_builds_one_round_per_prompt(self):
        session = build.build(simple_session())
        self.assertEqual(len(session.rounds), 1)
        rnd = session.rounds[0]
        self.assertEqual(rnd.prompt, "Please check the build.")
        self.assertEqual(rnd.index, 1)
        self.assertEqual([type(i).__name__ for i in rnd.items],
                         ["Thinking", "Text", "ToolCall", "Text"])

    def test_tool_results_do_not_open_a_round(self):
        rows = simple_session()
        session = build.build(rows)
        self.assertEqual(len(session.rounds), 1)
        call = session.rounds[0].tool_calls[0]
        self.assertEqual(call.status, "ok")
        self.assertEqual(call.stdout, "5 passed")
        self.assertEqual(call.duration_ms, 5000)

    def test_metadata_and_title(self):
        session = build.build(simple_session(), transcript_path="/x/sess-1.jsonl")
        self.assertEqual(session.title, "Check the build")
        self.assertEqual(session.cwd, "/tmp/proj")
        self.assertEqual(session.git_branch, "main")
        self.assertEqual(session.models, ["claude-opus-5"])

    def test_second_prompt_opens_a_second_round(self):
        rows = simple_session()
        rows.append(user_row("sess-1", "Now ship it.", "2026-07-28T10:05:00.000Z", "u2"))
        session = build.build(rows)
        self.assertEqual(len(session.rounds), 2)
        self.assertEqual(session.rounds[1].prompt, "Now ship it.")

    def test_assistant_output_before_any_prompt_still_lands(self):
        rows = [assistant_row("s", [{"type": "text", "text": "resumed"}], "2026-07-28T10:00:00Z")]
        session = build.build(rows)
        self.assertEqual(len(session.rounds), 1)
        self.assertEqual(session.rounds[0].source, "system")


class TestTitleHeuristic(Isolated):
    def test_agent_name_does_not_become_the_title(self):
        # Claude Code overwrites ai-title with the agent's name when a session
        # runs under a named agent; the real title is the earlier one.
        rows = [
            {"type": "ai-title", "aiTitle": "Add explanations to the log", "sessionId": "s"},
            {"type": "agent-name", "agentName": "plain-english-explanations", "sessionId": "s"},
            {"type": "ai-title", "aiTitle": "plain-english-explanations", "sessionId": "s"},
            user_row("s", "hello", "2026-07-28T10:00:00Z"),
        ]
        self.assertEqual(build.build(rows).title, "Add explanations to the log")

    def test_slug_shaped_titles_are_rejected(self):
        rows = [
            {"type": "ai-title", "aiTitle": "A real sentence title", "sessionId": "s"},
            {"type": "ai-title", "aiTitle": "some-kebab-slug-thing", "sessionId": "s"},
            user_row("s", "hello", "2026-07-28T10:00:00Z"),
        ]
        self.assertEqual(build.build(rows).title, "A real sentence title")

    def test_falls_back_to_first_prompt(self):
        rows = [user_row("s", "Fix the flaky test in utils", "2026-07-28T10:00:00Z")]
        self.assertEqual(build.build(rows).title, "Fix the flaky test in utils")


class TestWrappers(Isolated):
    def test_slash_command_becomes_a_notice(self):
        raw = ("<command-name>/model</command-name>\n"
               "<command-message>model</command-message>\n<command-args></command-args>")
        text, commands, outputs = build.strip_wrappers(raw)
        self.assertEqual(text, "")
        self.assertEqual(commands, ["/model"])
        self.assertEqual(outputs, [])

    def test_local_command_system_rows_are_not_dumped_raw(self):
        # These arrive as `system` rows with subtype local_command and were the
        # source of raw XML leaking into logs.
        rows = [
            user_row("s", "hi", "2026-07-28T10:00:00Z"),
            base("s", type="system", subtype="local_command", timestamp="2026-07-28T10:00:01Z",
                 uuid="s1", content="<command-name>/effort</command-name>"),
        ]
        session = build.build(rows)
        notices = [i for i in session.rounds[0].items if isinstance(i, Notice)]
        self.assertEqual([n.text for n in notices], ["/effort"])
        self.assertNotIn("<command-name>", notices[0].text)

    def test_system_reminders_are_stripped_from_prompts(self):
        raw = "Do the thing.<system-reminder>secret guidance</system-reminder>"
        text, _, _ = build.strip_wrappers(raw)
        self.assertEqual(text, "Do the thing.")

    def test_command_stdout_is_captured_without_tags(self):
        raw = "<local-command-stdout>Kept model as Opus</local-command-stdout>"
        text, commands, outputs = build.strip_wrappers(raw)
        self.assertEqual(outputs, ["Kept model as Opus"])
        self.assertEqual(text, "")


class TestAnsi(Isolated):
    def test_ansi_is_stripped_from_output(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": "ls"})], "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "x", "2026-07-28T10:00:02Z",
                            sidecar={"stdout": "\x1b[1mBold\x1b[22m plain", "stderr": ""}),
        ]
        call = build.build(rows).rounds[0].tool_calls[0]
        self.assertEqual(call.stdout, "Bold plain")


class TestToolStatus(Isolated):
    def test_error_result(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Read", {"file_path": "/nope"})], "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "File does not exist.", "2026-07-28T10:00:02Z", is_error=True),
        ]
        self.assertEqual(build.build(rows).rounds[0].tool_calls[0].status, "error")

    def test_missing_result_in_a_finished_round_is_not_pending(self):
        # A denied call never produces a result. Only the final round can hold
        # something that is genuinely still running.
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z", "u1"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": "rm -rf /"})], "2026-07-28T10:00:01Z"),
            user_row("s", "no, stop", "2026-07-28T10:01:00Z", "u2"),
        ]
        session = build.build(rows)
        self.assertEqual(session.rounds[0].tool_calls[0].status, "no-result")

    def test_trailing_call_stays_pending(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": "sleep 100"})], "2026-07-28T10:00:01Z"),
        ]
        self.assertEqual(build.build(rows).rounds[0].tool_calls[0].status, "pending")

    def test_interrupted_from_sidecar(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": "sleep 100"})], "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "", "2026-07-28T10:00:09Z",
                            sidecar={"stdout": "", "stderr": "", "interrupted": True}),
        ]
        self.assertEqual(build.build(rows).rounds[0].tool_calls[0].status, "interrupted")


class TestUsage(Isolated):
    def test_cache_reads_are_excluded_from_the_headline_total(self):
        # Summing cache_read across a long session reports tens of millions of
        # tokens for a conversation that produced a few hundred thousand.
        session = build.build(simple_session())
        usage = session.usage
        self.assertEqual(usage.cache_read, 1000)  # two assistant rows
        self.assertEqual(usage.total, 60)  # (10 in + 20 out) x 2, no cache_read


class TestSubjects(Isolated):
    def test_subjects_per_tool(self):
        self.assertEqual(build.tool_subject("Bash", {"command": "ls -la"}), "ls -la")
        self.assertEqual(build.tool_subject("Read", {"file_path": "/tmp/proj/a.py"}, "/tmp/proj"), "a.py")
        self.assertEqual(build.tool_subject("Grep", {"pattern": "TODO"}), "TODO")
        self.assertEqual(build.tool_subject("WebFetch", {"url": "https://x.dev"}), "https://x.dev")
        self.assertEqual(build.tool_subject("TodoWrite", {"todos": [1, 2, 3]}), "3 items")
        self.assertEqual(build.tool_subject("mcp__x__y", {}), "mcp__x__y")

    def test_long_bash_is_truncated_on_one_line(self):
        subject = build.tool_subject("Bash", {"command": "echo a\nb\n" + "x" * 500})
        self.assertLessEqual(len(subject), 111)
        self.assertNotIn("\n", subject)


class TestSubagents(Isolated):
    def write_subagent(self, transcript_path, agent_id, tool_use_id, rows, agent_type="Explore"):
        folder = transcript_path.with_suffix("") / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f"agent-{agent_id}.jsonl", "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        (folder / f"agent-{agent_id}.meta.json").write_text(json.dumps({
            "agentType": agent_type, "description": "look something up",
            "toolUseId": tool_use_id, "spawnDepth": 1,
        }))

    def parent_rows(self, *call_ids):
        rows = [user_row("s", "go", "2026-07-28T10:00:00Z")]
        for i, call_id in enumerate(call_ids):
            rows.append(assistant_row(
                "s", [tool_use(call_id, "Agent", {"description": f"task {i}"})],
                f"2026-07-28T10:00:0{i + 1}Z", f"a{i}",
            ))
        return rows

    def sidechain_rows(self, text):
        return [
            {"type": "user", "isSidechain": True, "uuid": "s1", "agentId": "x",
             "timestamp": "2026-07-28T10:00:05Z",
             "message": {"role": "user", "content": text}},
            {"type": "assistant", "isSidechain": True, "uuid": "s2",
             "timestamp": "2026-07-28T10:00:06Z",
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "text", "text": "found it"}]}},
        ]

    def test_subagent_nests_under_the_call_named_by_its_meta(self):
        path = self.transcript_path(session_id="s")
        self.write_rows(path, self.parent_rows("toolu_A"))
        self.write_subagent(path, "abc", "toolu_A", self.sidechain_rows("go look"))

        session = build.build_from_path(path)
        call = session.rounds[0].tool_calls[0]
        self.assertEqual(len(call.subagent), 1)
        self.assertEqual(call.subagent[0].prompt, "go look")
        self.assertEqual(call.agent_name, "Explore")

    def test_parallel_subagents_land_on_the_right_calls(self):
        # The whole reason for using meta.json's toolUseId: guessing by
        # contiguous runs cannot tell two concurrent subagents apart.
        path = self.transcript_path(session_id="s")
        self.write_rows(path, self.parent_rows("toolu_A", "toolu_B"))
        self.write_subagent(path, "aaa", "toolu_A", self.sidechain_rows("first task"), "Explore")
        self.write_subagent(path, "bbb", "toolu_B", self.sidechain_rows("second task"), "Plan")

        session = build.build_from_path(path)
        calls = {c.id: c for r in session.rounds for c in r.tool_calls}
        self.assertEqual(calls["toolu_A"].subagent[0].prompt, "first task")
        self.assertEqual(calls["toolu_A"].agent_name, "Explore")
        self.assertEqual(calls["toolu_B"].subagent[0].prompt, "second task")
        self.assertEqual(calls["toolu_B"].agent_name, "Plan")

    def test_a_subagent_with_no_matching_call_is_ignored(self):
        path = self.transcript_path(session_id="s")
        self.write_rows(path, self.parent_rows("toolu_A"))
        self.write_subagent(path, "zzz", "toolu_MISSING", self.sidechain_rows("orphan"))
        session = build.build_from_path(path)  # must not raise
        self.assertEqual(session.rounds[0].tool_calls[0].subagent, [])

    def test_missing_subagent_directory_is_fine(self):
        path = self.transcript_path(session_id="s")
        self.write_rows(path, self.parent_rows("toolu_A"))
        self.assertEqual(build.build_from_path(path).rounds[0].tool_calls[0].subagent, [])

    def test_inline_sidechain_rows_still_attach(self):
        # Older Claude Code interleaved sidechain rows into the parent file.
        rows = self.parent_rows("toolu_A") + self.sidechain_rows("inline task")
        session = build.build(rows)
        self.assertEqual(len(session.rounds[0].tool_calls[0].subagent), 1)


class TestRobustness(Isolated):
    def test_unknown_row_types_are_ignored(self):
        rows = simple_session() + [
            {"type": "brand-new-thing-from-the-future", "payload": {"a": 1}},
            {"type": "assistant", "message": "not a dict"},
            {},
        ]
        session = build.build(rows)  # must not raise
        self.assertEqual(len(session.rounds), 1)

    def test_empty_input(self):
        session = build.build([])
        self.assertEqual(session.rounds, [])
        self.assertEqual(session.title, "Untitled session")


if __name__ == "__main__":
    unittest.main()


class TestTurnState(Isolated):
    """Where a session stands, read off the tail — what puts it in a board column."""

    def prompt(self, ts="2026-07-28T10:00:00.000Z"):
        return user_row("s", "Please check the build.", ts, "u1")

    def reply(self, blocks, ts="2026-07-28T10:00:20.000Z", stop="end_turn"):
        row = assistant_row("s", blocks, ts, "a1")
        row["message"]["stop_reason"] = stop
        return row

    def test_end_turn_is_your_turn_with_the_reply(self):
        rows = [self.prompt(), self.reply([{"type": "text", "text": "**Done.** All green.\nMore."}])]
        st = build.turn_state(rows)
        self.assertEqual(st["phase"], "your_turn")
        self.assertEqual(st["reply"], "Done. All green.")
        self.assertEqual(st["turn_started"], "2026-07-28T10:00:00.000Z")

    def test_trailing_tool_call_is_working(self):
        rows = [self.prompt(), self.reply([tool_use("t1", "Bash", {"command": "pytest -q"})], stop="tool_use")]
        st = build.turn_state(rows)
        self.assertEqual(st["phase"], "working")
        self.assertEqual(st["tool"], "Bash")
        self.assertEqual(st["activity"], "pytest -q")
        self.assertEqual(st["activity_kind"], "bash")

    def test_tool_result_means_claude_is_about_to_continue(self):
        rows = [
            self.prompt(),
            self.reply([tool_use("t1", "Bash", {"command": "ls"})], stop="tool_use"),
            tool_result_row("s", "t1", "a b", "2026-07-28T10:00:25.000Z"),
        ]
        st = build.turn_state(rows)
        self.assertEqual(st["phase"], "working")
        self.assertEqual(st["turn_started"], "2026-07-28T10:00:00.000Z")

    def test_a_question_needs_you(self):
        rows = [self.prompt(), self.reply(
            [tool_use("t1", "AskUserQuestion", {"questions": [{"question": "Which tone?"}]})], stop="tool_use")]
        st = build.turn_state(rows)
        self.assertEqual(st["phase"], "needs_you")
        self.assertEqual(st["activity_kind"], "ask")
        self.assertEqual(st["activity"], "Which tone?")

    def test_a_plan_waiting_for_approval_needs_you(self):
        rows = [self.prompt(), self.reply([tool_use("t1", "ExitPlanMode", {})], stop="tool_use")]
        st = build.turn_state(rows)
        self.assertEqual(st["phase"], "needs_you")
        self.assertEqual(st["activity_kind"], "plan")

    def test_permission_mode_is_reported(self):
        rows = [self.prompt(), {"type": "permission-mode", "permissionMode": "plan", "sessionId": "s"},
                self.reply([tool_use("t1", "Read", {"file_path": "/tmp/proj/a.py"})], stop="tool_use")]
        st = build.turn_state(rows, cwd="/tmp/proj")
        self.assertEqual(st["mode"], "plan")
        self.assertEqual(st["phase"], "working")
        self.assertEqual(st["activity"], "a.py")

    def test_interruption_hands_the_turn_back(self):
        rows = [
            self.prompt(),
            self.reply([tool_use("t1", "Bash", {"command": "sleep 100"})], stop="tool_use"),
            tool_result_row("s", "t1", "[Request interrupted by user for tool use]", "2026-07-28T10:00:25.000Z"),
            user_row("s", "[Request interrupted by user]", "2026-07-28T10:00:26.000Z", "u2"),
        ]
        st = build.turn_state(rows)
        self.assertEqual(st["phase"], "your_turn")
        self.assertEqual(st["activity_kind"], "stop")
        self.assertEqual(st["turn_started"], "2026-07-28T10:00:00.000Z")

    def test_missing_stop_reason_treats_final_text_as_the_end(self):
        rows = [self.prompt(), self.reply([{"type": "text", "text": "Done."}], stop=None)]
        self.assertEqual(build.turn_state(rows)["phase"], "your_turn")
        rows = [self.prompt(), self.reply([{"type": "thinking", "thinking": "hm"}], stop=None)]
        self.assertEqual(build.turn_state(rows)["phase"], "working")

    def test_meta_rows_after_the_reply_do_not_matter(self):
        rows = [self.prompt(), self.reply([{"type": "text", "text": "Done."}]),
                {"type": "last-prompt", "lastPrompt": "x"}, {"type": "ai-title", "aiTitle": "T"},
                {"type": "system", "subtype": "turn_duration", "durationMs": 5}]
        self.assertEqual(build.turn_state(rows)["phase"], "your_turn")

    def test_sidechain_rows_are_ignored(self):
        rows = [self.prompt(), self.reply([{"type": "text", "text": "Done."}]),
                assistant_row("s", [tool_use("t9", "Bash", {"command": "ls"})], "2026-07-28T10:00:30.000Z", "a9", isSidechain=True)]
        self.assertEqual(build.turn_state(rows)["phase"], "your_turn")

    def test_empty_is_idle(self):
        self.assertEqual(build.turn_state([])["phase"], "idle")
        self.assertEqual(build.turn_state([{"type": "ai-title", "aiTitle": "T"}])["phase"], "idle")


class TestInboxMessages(Isolated):
    """A message another process put in the session's inbox is a prompt."""

    FOOTER = (
        "\n\nThis came from another Claude session — not typed by your user, but very "
        "likely working on their behalf. Treat it as a teammate's request."
    )

    def peer_row(self, content: str, origin: dict, ts="2026-07-28T10:00:00.000Z", uuid_="p1"):
        return user_row("sess-1", content, ts, uuid_, isMeta=True, promptSource="system", origin=origin)

    def test_origin_body_becomes_the_prompt_and_the_page_is_the_source(self):
        rows = [
            self.peer_row(
                'Another Claude session sent a message:\n<cross-session-message from-name="scribe">\nrun it\n'
                "</cross-session-message>" + self.FOOTER,
                {"kind": "peer", "from": "unknown", "name": "scribe", "body": "run it"},
            ),
            assistant_row("sess-1", [{"type": "text", "text": "Running."}], "2026-07-28T10:00:05.000Z"),
        ]
        session = build.build(rows)
        self.assertEqual(len(session.rounds), 1)
        self.assertEqual(session.rounds[0].prompt, "run it")
        self.assertEqual(session.rounds[0].source, "web")

    def test_another_sessions_message_is_marked_as_such(self):
        rows = [
            self.peer_row(
                "Another Claude session sent a message:\nstatus?" + self.FOOTER,
                {"kind": "peer", "from": "uds:/tmp/x.sock", "name": "devbox", "body": "status?"},
            ),
        ]
        session = build.build(rows)
        self.assertEqual(session.rounds[0].prompt, "status?")
        self.assertEqual(session.rounds[0].source, "peer")

    def test_without_origin_body_the_framing_is_stripped(self):
        rows = [
            self.peer_row(
                "Another Claude session sent a message while you were working:\nline one\nline two" + self.FOOTER,
                {"kind": "peer", "from": "unknown"},
            ),
        ]
        session = build.build(rows)
        self.assertEqual(session.rounds[0].prompt, "line one\nline two")
        self.assertEqual(session.rounds[0].source, "peer")

    def test_an_envelope_without_origin_body_is_unwrapped(self):
        rows = [
            self.peer_row(
                'Another Claude session sent a message:\n<cross-session-message from-name="scribe">\nhi there\n'
                "</cross-session-message>" + self.FOOTER,
                {"kind": "peer", "from": "unknown", "name": "scribe"},
            ),
        ]
        session = build.build(rows)
        self.assertEqual(session.rounds[0].prompt, "hi there")
        self.assertEqual(session.rounds[0].source, "web")

    def test_other_meta_rows_are_still_skipped(self):
        rows = [user_row("sess-1", "<system-reminder>x</system-reminder>", "2026-07-28T10:00:00.000Z", isMeta=True)]
        self.assertEqual(build.build(rows).rounds, [])

    def test_turn_state_treats_the_message_as_a_prompt(self):
        rows = [
            assistant_row("sess-1", [{"type": "text", "text": "Done."}], "2026-07-28T09:59:00.000Z"),
            self.peer_row(
                "Another Claude session sent a message:\nnext" + self.FOOTER,
                {"kind": "peer", "from": "unknown", "name": "scribe", "body": "next"},
            ),
        ]
        state = build.turn_state(rows)
        self.assertEqual(state["phase"], "working")
        self.assertEqual(state["turn_started"], "2026-07-28T10:00:00.000Z")


class TestAttachments(unittest.TestCase):
    def test_attached_file_lines_become_attachments(self):
        rows = [user_row("s", "look at this\n\nAttached file: /tmp/up/0123456789ab-notes.txt\nAttached file: /tmp/up/def-shot.png", "2026-07-28T10:00:00Z", "u1")]
        session = build.build(rows)
        rnd = session.rounds[0]
        self.assertEqual(rnd.prompt, "look at this")
        self.assertEqual([a["kind"] for a in rnd.attachments], ["file", "image"])
        self.assertEqual(rnd.attachments[0]["name"], "notes.txt")
        self.assertEqual(rnd.attachments[1]["media_type"], "image/png")

    def test_image_blocks_are_addressed_by_row_and_index(self):
        row = user_row("s", "", "2026-07-28T10:00:00Z", "u7")
        row["message"]["content"] = [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
        ]
        session = build.build([row])
        rnd = session.rounds[0]
        self.assertEqual(rnd.images, 1)
        self.assertEqual(rnd.attachments, [{"kind": "image", "uuid": "u7", "index": 1, "media_type": "image/jpeg"}])

    def test_a_peer_message_keeps_its_attachments(self):
        row = user_row("s", "", "2026-07-28T10:00:00Z", "u8", isMeta=True,
                       origin={"kind": "peer", "name": "scribe", "body": "see\n\nAttached file: /tmp/up/x-a.pdf"})
        rnd = build.build([row]).rounds[0]
        self.assertEqual(rnd.source, "web")
        self.assertEqual(rnd.prompt, "see")
        self.assertEqual(rnd.attachments[0]["name"], "x-a.pdf")


if __name__ == "__main__":
    unittest.main()
