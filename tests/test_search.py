"""Global full-text search across conversations."""

from __future__ import annotations

import unittest

from helpers import Isolated, assistant_row, simple_session, tool_use, user_row

from scribe import archive, search, transcript


class SearchCase(Isolated):
    def setUp(self):
        super().setUp()
        search.reset()  # the singleton would otherwise hold the previous temp dir
        self.index = search.SearchIndex()

    def tearDown(self):
        search.reset()
        super().tearDown()

    def make(self, session_id, prompt, reply="ok", command=None, cwd="/tmp/proj"):
        rows = [user_row(session_id, prompt, "2026-07-28T10:00:00Z", "u1", cwd=cwd)]
        blocks = [{"type": "text", "text": reply}]
        if command:
            blocks.append(tool_use("t-" + session_id, "Bash", {"command": command}))
        rows.append(assistant_row(session_id, blocks, "2026-07-28T10:00:05Z", "a1", cwd=cwd))
        path = self.transcript_path(cwd=cwd, session_id=session_id)
        self.write_rows(path, rows)
        return path


class TestQueryParsing(Isolated):
    def test_plain_words_become_quoted_terms(self):
        self.assertEqual(search.fts_query("hello world"), '"hello" "world"')

    def test_quoted_phrases_stay_together(self):
        self.assertEqual(search.fts_query('"exact phrase" other'), '"exact phrase" "other"')

    def test_prefix_search_is_preserved(self):
        self.assertEqual(search.fts_query("rev*"), '"rev"*')

    def test_fts_operators_cannot_cause_a_syntax_error(self):
        # `rm -rf`, `a:b`, `NEAR(`, a lone quote — all of these are FTS5
        # syntax and would raise mid-keystroke if passed through.
        for raw in ["rm -rf", "a:b", "NEAR(", 'a "', "*", "-", "AND OR NOT", "foo^bar"]:
            query = search.fts_query(raw)
            self.assertNotIn("\x00", query)
            if query:
                self.assertEqual(query.count('"') % 2, 0, raw)

    def test_empty_input(self):
        self.assertEqual(search.fts_query(""), "")
        self.assertEqual(search.fts_query("   "), "")
        self.assertEqual(search.fts_query("!!!"), "")


class TestIndexing(SearchCase):
    def test_finds_a_prompt(self):
        self.make("s1", "how do I fix the flaky test")
        self.index.sync()
        result = self.index.search("flaky")
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["id"], "s1")

    def test_finds_assistant_text_and_tool_commands(self):
        self.make("s1", "hello", reply="the answer is quarto", command="grep -r needle .")
        self.index.sync()
        self.assertEqual(self.index.search("quarto")["total"], 1)
        self.assertEqual(self.index.search("needle")["total"], 1)

    def test_results_are_grouped_by_conversation(self):
        # A "hit" is a matching document — a prompt, a reply, a tool call — not
        # a term occurrence. So s1's prompt and reply are two hits, and a second
        # mention inside the same prompt does not add a third.
        self.make("s1", "widget widget in the prompt", reply="widget in the reply")
        self.make("s2", "widget two")
        self.index.sync()
        result = self.index.search("widget")
        self.assertEqual(len(result["sessions"]), 2)
        self.assertEqual(result["total"], 3)
        by_id = {s["id"]: s for s in result["sessions"]}
        self.assertEqual(by_id["s1"]["hits"], 2)
        self.assertEqual(by_id["s2"]["hits"], 1)

    def test_snippets_mark_the_hit(self):
        self.make("s1", "the isotonic regression solver")
        self.index.sync()
        snippet = self.index.search("isotonic")["sessions"][0]["matches"][0]["snippet"]
        self.assertIn("\x02isotonic\x03", snippet)

    def test_matches_carry_a_round_number_to_jump_to(self):
        rows = simple_session("s1")
        rows.append(user_row("s1", "now find the needle", "2026-07-28T11:00:00Z", "u2"))
        path = self.transcript_path(session_id="s1")
        self.write_rows(path, rows)
        self.index.sync()
        match = self.index.search("needle")["sessions"][0]["matches"][0]
        self.assertEqual(match["round"], 2)
        self.assertEqual(match["kind"], "prompt")

    def test_no_matches_is_not_an_error(self):
        self.make("s1", "hello")
        self.index.sync()
        result = self.index.search("nonexistentterm")
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["total"], 0)
        self.assertNotIn("error", result)


class TestIncrementalSync(SearchCase):
    def test_unchanged_sessions_are_skipped(self):
        self.make("s1", "hello")
        first = self.index.sync()
        self.assertEqual(first["indexed"], 1)
        second = self.index.sync()
        self.assertEqual(second["indexed"], 0)
        self.assertEqual(second["skipped"], 1)

    def test_new_content_becomes_searchable(self):
        path = self.make("s1", "hello")
        self.index.sync()
        self.assertEqual(self.index.search("pomegranate")["total"], 0)

        self.append_rows(path, [user_row("s1", "now about pomegranate", "2026-07-28T12:00:00Z", "u2")])
        self.index.sync()
        self.assertEqual(self.index.search("pomegranate")["total"], 1)

    def test_reindexing_replaces_rather_than_duplicates(self):
        path = self.make("s1", "unique-token here")
        self.index.sync()
        self.append_rows(path, [user_row("s1", "more", "2026-07-28T12:00:00Z", "u2")])
        self.index.sync()
        self.assertEqual(self.index.search("unique-token")["total"], 1)

    def test_a_vanished_session_is_pruned(self):
        import os

        path = self.make("s1", "ephemeral")
        self.index.sync()
        self.assertEqual(self.index.search("ephemeral")["total"], 1)
        os.unlink(path)
        self.index.sync()
        self.assertEqual(self.index.search("ephemeral")["total"], 0)


class TestArchivedSessions(SearchCase):
    def test_archived_sessions_remain_searchable(self):
        # The whole point: a conversation Claude Code deleted must still be
        # findable, which is what makes the archive worth keeping.
        import os

        path = self.make("s1", "the memorable incident")
        archive.sweep()
        os.unlink(path)

        self.index.sync()
        result = self.index.search("memorable")
        self.assertEqual(len(result["sessions"]), 1)
        self.assertTrue(result["sessions"][0]["archived"])


class TestSubagentContent(SearchCase):
    def test_subagent_conversations_are_searchable(self):
        path = self.transcript_path(session_id="s1")
        self.write_rows(path, [
            user_row("s1", "delegate this", "2026-07-28T10:00:00Z", "u1"),
            assistant_row("s1", [tool_use("toolu_A", "Agent", {"description": "go"})],
                          "2026-07-28T10:00:01Z", "a1"),
        ])
        folder = path.with_suffix("") / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-x.jsonl").write_text(
            '{"type":"user","isSidechain":true,"uuid":"s1","timestamp":"2026-07-28T10:00:02Z",'
            '"message":{"role":"user","content":"look for the buried treasure"}}\n'
        )
        (folder / "agent-x.meta.json").write_text('{"agentType":"Explore","toolUseId":"toolu_A"}')

        self.index.sync()
        result = self.index.search("treasure")
        self.assertEqual(len(result["sessions"]), 1)
        self.assertTrue(result["sessions"][0]["matches"][0]["kind"].startswith("subagent:"))


class TestOverview(SearchCase):
    def rows(self, session_id, stamps, model="claude-opus-5", cwd="/tmp/proj"):
        out = []
        for i, ts in enumerate(stamps):
            out.append(user_row(session_id, f"prompt {i}", ts, f"u{i}", cwd=cwd))
            out.append(assistant_row(session_id, [{"type": "text", "text": "reply"}, tool_use(f"t{i}", "Bash", {"command": "ls"})],
                                     ts, f"a{i}", cwd=cwd, usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 9000}))
            out[-1]["message"]["model"] = model
        return out

    def test_buckets_streaks_and_models(self):
        from datetime import date, timedelta

        today = date(2026, 9, 15)
        def at(d, hour=21):
            return f"{d.isoformat()}T{hour:02d}:00:00Z"
        self.write_rows(self.transcript_path(session_id="s1"), self.rows("s1", [at(today), at(today, 9), at(today - timedelta(days=1))]))
        self.write_rows(self.transcript_path(session_id="s2"), self.rows("s2", [at(today - timedelta(days=2)), at(today - timedelta(days=40))], model="claude-haiku-4-5"))
        self.index.sync()
        # Everything is stamped in UTC; local buckets may shift a day at the
        # edges, so assert on totals and shapes, not on the exact day keys.
        allt = self.index.overview(None, today=today + timedelta(days=1))
        self.assertEqual(allt["sessions"], 2)
        self.assertEqual(allt["prompts"], 5)
        self.assertEqual(allt["replies"], 5)
        self.assertEqual(allt["tool_calls"], 5)
        self.assertEqual(allt["tokens"], 5 * 150)  # cache reads excluded
        self.assertEqual({m["model"] for m in allt["models"]}, {"claude-opus-5", "claude-haiku-4-5"})
        self.assertEqual(allt["favourite_model"], "claude-opus-5")
        self.assertEqual(allt["models"][0]["sessions"], 1)
        self.assertGreaterEqual(allt["longest_streak"], 2)
        self.assertEqual(len(allt["grid"]), 52 * 7 + (today + timedelta(days=1)).weekday() + 1)
        self.assertEqual(sum(g["p"] for g in allt["grid"]), 5)
        recent = self.index.overview(7, today=today + timedelta(days=1))
        self.assertEqual(recent["prompts"], 4)
        self.assertEqual(recent["sessions"], 2)
        self.assertEqual(recent["range"], 7)
        self.assertLessEqual(len(recent["grid"]), 7)
        self.assertIsNotNone(allt["peak_hour"])

    def test_an_empty_index_is_a_quiet_overview(self):
        empty = self.index.overview(30)
        self.assertEqual(empty["sessions"], 0)
        self.assertEqual(empty["current_streak"], 0)
        self.assertEqual(empty["models"], [])
        self.assertIsNone(empty["peak_hour"])


class TestStats(SearchCase):
    def test_stats_report_the_corpus(self):
        self.make("s1", "one")
        self.make("s2", "two")
        self.index.sync()
        stats = self.index.stats()
        self.assertEqual(stats["sessions"], 2)
        self.assertGreater(stats["documents"], 0)
        self.assertGreater(stats["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
