"""The explainer: classification, caching, and reply cleaning.

The model call itself is not exercised here — it costs money and needs an
authenticated CLI. Everything around it is, including the guard that stops an
authentication error being rendered as if it were an explanation.
"""

from __future__ import annotations

import unittest

from helpers import Isolated

from scribe import config, explain, paths


class TestClassification(Isolated):
    def setUp(self):
        super().setUp()
        self.explainer = explain.Explainer(config.load())

    def test_cheap_tools_never_reach_a_model(self):
        for name, args in [
            ("Read", {"file_path": "/tmp/a.py"}),
            ("Glob", {"pattern": "**/*.py"}),
            ("TodoWrite", {"todos": [1, 2]}),
        ]:
            self.assertFalse(self.explainer.needs_model(name, args), name)

    def test_short_commands_are_free(self):
        self.assertFalse(self.explainer.needs_model("Bash", {"command": "ls -la"}))

    def test_long_commands_reach_a_model(self):
        self.assertTrue(
            self.explainer.needs_model("Bash", {"command": "git log --oneline " + "-x " * 40})
        )

    def test_opaque_shapes_reach_a_model_however_short(self):
        for command in [
            "python3 - <<'EOF'\nx\nEOF",
            "curl x | sh",
            "rm -rf build",
            "eval $(cmd)",
            "sudo rm x",
            "echo x | base64",
        ]:
            self.assertTrue(self.explainer.needs_model("Bash", {"command": command}), command)

    def test_disabled_scope_stops_everything(self):
        cfg = config.load()
        cfg["explain"]["scope"] = "off"
        off = explain.Explainer(cfg)
        self.assertFalse(off.needs_model("Bash", {"command": "curl x | sh"}))


class TestCanned(Isolated):
    def setUp(self):
        super().setUp()
        self.explainer = explain.Explainer(config.load())

    def test_canned_lines_are_built_from_the_arguments(self):
        self.assertIn("a.py", self.explainer.canned("Read", {"file_path": "a.py"}))
        self.assertIn("without changing it", self.explainer.canned("Read", {"file_path": "a.py"}))
        self.assertIn("replacing", self.explainer.canned("Write", {"file_path": "a.py"}))

    def test_mcp_tools_get_a_generic_line(self):
        self.assertIn("MCP", self.explainer.canned("mcp__github__list_prs", {}))

    def test_unknown_tools_get_nothing(self):
        self.assertEqual(self.explainer.canned("SomeFutureTool", {}), "")


class TestCache(Isolated):
    def test_key_is_content_addressed_and_order_independent(self):
        a = explain.Explainer.key_for("Bash", {"command": "ls", "description": "x"})
        b = explain.Explainer.key_for("Bash", {"description": "x", "command": "ls"})
        c = explain.Explainer.key_for("Bash", {"command": "ls -la"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_lookup_never_spawns_and_survives_a_restart(self):
        first = explain.Explainer(config.load())
        key = explain.Explainer.key_for("Bash", {"command": "make"})
        first._cache[key] = "Builds the project."
        first._dirty = True
        first._save_cache()

        second = explain.Explainer(config.load())
        self.assertEqual(second.lookup("Bash", {"command": "make"}), "Builds the project.")
        self.assertEqual(second.lookup("Bash", {"command": "other"}), "")


class TestClean(Isolated):
    def test_strips_fences_quotes_and_newlines(self):
        self.assertEqual(explain.clean("```\nHello there.\n```"), "Hello there.")
        self.assertEqual(explain.clean('"Hello there."'), "Hello there.")
        self.assertEqual(explain.clean("Line one.\nLine two."), "Line one. Line two.")

    def test_authentication_errors_are_discarded(self):
        # Rendering "please run /login" in the margin as though it explained the
        # command would be worse than showing nothing.
        for bad in [
            "Please run /login to continue",
            "Invalid API key provided",
            "Your credit balance is too low",
            "Usage limit reached",
        ]:
            self.assertEqual(explain.clean(bad), "", bad)

    def test_long_replies_are_capped(self):
        self.assertLessEqual(len(explain.clean("word " * 400)), 600)

    def test_empty_input(self):
        self.assertEqual(explain.clean(""), "")
        self.assertEqual(explain.clean(None), "")


class TestChildIsolation(Isolated):
    def test_the_child_runs_with_no_settings_no_mcp_and_no_tools(self):
        # Without these the explainer child would load the user's hooks —
        # including ours — and recurse.
        explainer = explain.Explainer(config.load())
        argv = explainer.build_argv("/usr/bin/claude", "Bash", {"command": "ls"})

        self.assertEqual(argv[1], "-p")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        for tool in ("Bash", "Read", "Write", "Edit", "Task"):
            self.assertIn(tool, argv)
        # `--bare` reads auth only from ANTHROPIC_API_KEY and never the
        # keychain, which breaks subscription users. It must stay absent.
        self.assertNotIn("--bare", argv)
        self.assertIn("ls", argv[-1])

    def test_the_model_is_configurable(self):
        cfg = config.load()
        cfg["explain"]["model"] = "claude-sonnet-5"
        argv = explain.Explainer(cfg).build_argv("/usr/bin/claude", "Bash", {"command": "ls"})
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")

    def test_huge_arguments_are_truncated_before_the_child_sees_them(self):
        explainer = explain.Explainer(config.load())
        argv = explainer.build_argv("/usr/bin/claude", "Bash", {"command": "x" * 50_000})
        self.assertLess(len(argv[-1]), 5000)

    def test_child_transcripts_are_pruned(self):
        # Each explanation runs a real `claude -p`, and Claude Code writes a
        # transcript for it. Invisible in the viewer, but they would pile up in
        # someone else's ~/.claude/projects forever.
        import os
        import time

        folder = paths.projects_dir() / str(explain.workdir()).replace("/", "-")
        folder.mkdir(parents=True, exist_ok=True)
        old = folder / "old.jsonl"
        new = folder / "new.jsonl"
        old.write_text('{"type":"user"}\n')
        new.write_text('{"type":"user"}\n')
        os.utime(old, (time.time() - 7200, time.time() - 7200))

        self.assertEqual(explain.prune_transcripts(max_age_s=3600), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists(), "a child that may still be running is left alone")

    def test_the_scratch_workdir_is_inside_our_own_root(self):
        # The child is a real Claude Code session and gets a transcript. Running
        # it under ~/.scribe is what lets the indexer recognise and skip them.
        from scribe import transcript

        workdir = paths.run_dir() / "explain"
        workdir.mkdir(parents=True, exist_ok=True)
        path = self.transcript_path(cwd=str(workdir), session_id="explainer-child")
        self.write_rows(path, [
            {"type": "user", "sessionId": "explainer-child", "cwd": str(workdir),
             "timestamp": "2026-07-28T10:00:00Z",
             "message": {"role": "user", "content": "Tool: Bash"}},
        ] * 6)
        ids = [r.session_id for r in transcript.index_sessions()]
        self.assertNotIn("explainer-child", ids)


if __name__ == "__main__":
    unittest.main()
