"""Markdown rendering, JSON rendering, and redaction."""

from __future__ import annotations

import unittest

from helpers import (
    Isolated,
    assistant_row,
    simple_session,
    tool_result_row,
    tool_use,
    user_row,
)

from scribe import build, config, redact, render_json, render_md


def render(rows, cfg=None):
    return render_md.render(build.build(rows), cfg or config.load())


class TestMarkdown(Isolated):
    def test_shape(self):
        text = render(simple_session())
        self.assertTrue(text.startswith("# Check the build\n"))
        self.assertIn("## 1 · You", text)
        self.assertIn("### Claude", text)
        self.assertIn("Please check the build.", text)
        self.assertIn("All **5** tests pass.", text)

    def test_no_marker_comments(self):
        # The previous generation embedded `<!-- agent-log turn=… -->` markers
        # because the viewer parsed the file back. Nothing parses it now, so the
        # document is free to be a document.
        text = render(simple_session())
        self.assertNotIn("<!--", text)

    def test_bash_command_and_output(self):
        text = render(simple_session())
        self.assertIn("```bash\npytest -q\n```", text)
        self.assertIn("5 passed", text)
        self.assertIn("<summary><b>Bash</b>", text)

    def test_thinking_is_foldable_and_optional(self):
        self.assertIn("I should run the tests first.", render(simple_session()))
        cfg = config.load()
        cfg["markdown"]["thinking"] = False
        self.assertNotIn("I should run the tests first.", render(simple_session(), cfg))

    def test_summary_mode_drops_bodies(self):
        cfg = config.load()
        cfg["markdown"]["tools"] = "summary"
        text = render(simple_session(), cfg)
        self.assertIn("**Bash**", text)
        self.assertNotIn("5 passed", text)

    def test_output_is_clipped_with_a_count(self):
        cfg = config.load()
        cfg["markdown"]["max_output_chars"] = 60
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": "seq 200"})], "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "x", "2026-07-28T10:00:02Z",
                            sidecar={"stdout": "\n".join(str(i) for i in range(200)), "stderr": ""}),
        ]
        text = render(rows, cfg)
        self.assertIn("more lines", text)

    def test_fences_survive_backticks_in_content(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": "echo '```'"})], "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "```", "2026-07-28T10:00:02Z",
                            sidecar={"stdout": "a ``` b", "stderr": ""}),
        ]
        text = render(rows)
        self.assertIn("````", text)

    def test_edit_renders_a_diff_with_a_stat(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Edit", {"file_path": "/tmp/proj/a.py"})],
                          "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "ok", "2026-07-28T10:00:02Z", sidecar={
                "filePath": "/tmp/proj/a.py",
                "structuredPatch": [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3,
                                     "lines": [" keep", "-gone", "+added", "+more"]}],
            }),
        ]
        text = render(rows)
        self.assertIn("```diff", text)
        self.assertIn("@@ -1,2 +1,3 @@", text)
        self.assertIn("+2 −1", text)

    def test_read_records_the_fact_not_the_file(self):
        # 782 Reads in one real session; inlining their contents would multiply
        # the log for no traceability gain, since the files are still on disk.
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Read", {"file_path": "/tmp/proj/big.py"})],
                          "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "\n".join(f"line {i}" for i in range(500)),
                            "2026-07-28T10:00:02Z"),
        ]
        text = render(rows)
        self.assertIn("read 499 lines", text)
        self.assertNotIn("line 400", text)

    def test_gap_divider_between_distant_rounds(self):
        rows = simple_session()
        rows.append(user_row("sess-1", "back again", "2026-07-28T14:00:00.000Z", "u2"))
        self.assertIn("later —", render(rows))

    def test_error_status_is_marked(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Read", {"file_path": "/nope"})], "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "File does not exist.", "2026-07-28T10:00:02Z", is_error=True),
        ]
        self.assertIn("error", render(rows))

    def test_html_in_a_summary_is_escaped(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": 'echo "<b>&</b>"'})],
                          "2026-07-28T10:00:01Z"),
        ]
        text = render(rows)
        self.assertIn("&lt;b&gt;", text)


class TestJson(Isolated):
    def test_items_carry_stable_keys_after_the_daemon_stamps_them(self):
        from scribe.daemon import _key_round

        session = build.build(simple_session())
        payload = _key_round(render_json.JsonRenderer().round(session.rounds[0]))
        keys = [i["key"] for i in payload["items"]]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertIn("t:toolu_A", keys)

    def test_tool_payload_carries_the_detail_the_markdown_edits_out(self):
        session = build.build(simple_session())
        call = [i for i in render_json.JsonRenderer().round(session.rounds[0])["items"]
                if i["kind"] == "tool"][0]
        self.assertEqual(call["command"], "pytest -q")
        self.assertEqual(call["stdout"], "5 passed")
        self.assertEqual(call["status"], "ok")
        self.assertEqual(call["tool_kind"], "bash")

    def test_write_content_is_not_duplicated_into_the_raw_arguments(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Write", {"file_path": "/a", "content": "X" * 500})],
                          "2026-07-28T10:00:01Z"),
        ]
        session = build.build(rows)
        call = [i for i in render_json.JsonRenderer().round(session.rounds[0])["items"]
                if i["kind"] == "tool"][0]
        self.assertEqual(len(call["content"]), 500)
        self.assertNotIn("content", call["input"])


class TestRedaction(Isolated):
    def setUp(self):
        super().setUp()
        self.scrub = redact.Redactor()

    def test_provider_keys(self):
        for secret in [
            "sk-ant-api03-" + "a" * 40,
            "ghp_" + "b" * 36,
            "AKIA" + "C" * 16,
            "xoxb-123456789012-abcdefghijkl",
        ]:
            self.assertNotIn(secret, self.scrub(f"export KEY={secret}"), secret)
            self.assertIn("[redacted]", self.scrub(f"export KEY={secret}"))

    def test_authorization_header_keeps_its_label(self):
        out = self.scrub("Authorization: Bearer abcdef1234567890")
        self.assertNotIn("abcdef1234567890", out)
        self.assertIn("Authorization: Bearer", out)

    def test_assignments(self):
        out = self.scrub('DATABASE_PASSWORD="hunter2hunter2"')
        self.assertNotIn("hunter2hunter2", out)

    def test_placeholders_are_left_alone(self):
        for innocuous in [
            "api_key = ${API_KEY}",
            "token: <your-token-here>",
            "password = xxxxxxxx",
            "secret: null",
        ]:
            self.assertEqual(self.scrub(innocuous), innocuous, innocuous)

    def test_private_keys(self):
        blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(self.scrub(blob), "[redacted]")

    def test_ordinary_prose_is_untouched(self):
        text = "The password reset flow needs a test, and the token bucket is fine."
        self.assertEqual(self.scrub(text), text)

    def test_nested_structures(self):
        data = {"env": {"OPENAI_API_KEY": "sk-" + "z" * 40}, "list": ["ghp_" + "y" * 36]}
        out = self.scrub.scrub_data(data)
        self.assertIn("[redacted]", out["env"]["OPENAI_API_KEY"])
        self.assertIn("[redacted]", out["list"][0])

    def test_disabled_is_a_pass_through(self):
        off = redact.Redactor(enabled=False)
        secret = "sk-ant-api03-" + "a" * 40
        self.assertEqual(off(secret), secret)

    def test_a_bad_custom_pattern_does_not_break_logging(self):
        scrub = redact.Redactor(extra_patterns=["([unclosed"])
        self.assertEqual(scrub("hello"), "hello")

    def test_the_trigger_prefilter_never_causes_a_miss(self):
        # scrub() skips its thirteen patterns when a cheap combined scan finds
        # no trigger substring. That is only sound if every secret shape
        # contains one, so assert it against a sample of each.
        samples = [
            "sk-ant-api03-" + "a" * 40,
            "sk-" + "b" * 40,
            "ghp_" + "c" * 36,
            "github_pat_" + "d" * 30,
            "xoxb-123456789012-abcdefghijkl",
            "AKIA" + "E" * 16,
            "AIza" + "f" * 35,
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
            "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
            "Authorization: Bearer abcdef1234567890",
            "X-API-Key: abcdef1234567890",
            "DATABASE_PASSWORD=hunter2hunter2",
            "client_secret: abcdef123456",
        ]
        for sample in samples:
            self.assertTrue(redact._TRIGGER.search(sample), f"no trigger in {sample[:30]}")
            self.assertIn("[redacted]", self.scrub(sample), sample[:40])

    def test_custom_patterns_bypass_the_prefilter(self):
        # A user pattern can match anything, so the fast path must be disabled.
        scrub = redact.Redactor(extra_patterns=[r"COMPANY-[0-9]{4}"])
        self.assertEqual(scrub("ref COMPANY-1234 here"), "ref [redacted] here")

    def test_redaction_reaches_the_markdown(self):
        rows = [
            user_row("s", "go", "2026-07-28T10:00:00Z"),
            assistant_row("s", [tool_use("t1", "Bash", {"command": "echo x"})], "2026-07-28T10:00:01Z"),
            tool_result_row("s", "t1", "x", "2026-07-28T10:00:02Z",
                            sidecar={"stdout": "ANTHROPIC_API_KEY=sk-ant-api03-" + "q" * 40,
                                     "stderr": ""}),
        ]
        text = render_md.render(build.build(rows), config.load(), redact.Redactor())
        self.assertNotIn("qqqq", text)
        self.assertIn("[redacted]", text)


class TestFormatting(Isolated):
    def test_durations(self):
        self.assertEqual(render_md.human_duration(500), "500ms")
        self.assertEqual(render_md.human_duration(5000), "5s")
        self.assertEqual(render_md.human_duration(90_000), "1m 30s")
        self.assertEqual(render_md.human_duration(3_600_000), "1h")
        self.assertEqual(render_md.human_duration(0), "")

    def test_tokens(self):
        self.assertEqual(render_md.human_tokens(999), "999")
        self.assertEqual(render_md.human_tokens(1500), "1.5k")
        self.assertEqual(render_md.human_tokens(2_500_000), "2.50M")

    def test_fence_sizing(self):
        self.assertEqual(render_md.fence_for("plain"), "```")
        self.assertEqual(render_md.fence_for("a ``` b"), "````")
        self.assertEqual(render_md.fence_for("a ````` b"), "``````")


if __name__ == "__main__":
    unittest.main()
