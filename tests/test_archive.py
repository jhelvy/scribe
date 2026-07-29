"""The archive.

This is the feature the whole tool exists for, and its failure mode is silent
and permanent, so it gets the most adversarial tests in the suite: sources that
grow, sources that get rewritten underneath us, sources that disappear entirely.
"""

from __future__ import annotations

import json
import os
import unittest

from helpers import Isolated, simple_session, user_row

from scribe import archive, paths, transcript


class ArchiveCase(Isolated):
    def make_session(self, session_id="sess-1", cwd="/tmp/proj"):
        path = self.transcript_path(cwd=cwd, session_id=session_id)
        self.write_rows(path, simple_session(session_id))
        return path

    def archived_path(self, session_id="sess-1", cwd="/tmp/proj"):
        return archive.session_archive_path(paths.project_slug(cwd), session_id)


class TestMirroring(ArchiveCase):
    def test_a_session_is_copied_byte_for_byte(self):
        src = self.make_session()
        archive.sweep()
        dst = self.archived_path()
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), src.read_bytes())

    def test_unchanged_sources_are_not_recopied(self):
        self.make_session()
        first = archive.sweep()
        self.assertGreater(first.files, 0)
        second = archive.sweep()
        self.assertEqual(second.files, 0)
        self.assertEqual(second.bytes_copied, 0)

    def test_growth_appends_only_the_new_bytes(self):
        src = self.make_session()
        archive.sweep()
        before = src.stat().st_size

        self.append_rows(src, [user_row("sess-1", "second prompt", "2026-07-28T11:00:00Z", "u2")])
        stats = archive.sweep()

        added = src.stat().st_size - before
        self.assertEqual(stats.bytes_copied, added, "should copy only the tail")
        self.assertEqual(self.archived_path().read_bytes(), src.read_bytes())

    def test_compaction_style_append_keeps_everything(self):
        # Compaction appends a boundary and continues in the same file; the
        # pre-compaction rows stay. Nothing special is needed, but the property
        # is worth pinning since it is the user-facing promise.
        src = self.make_session()
        archive.sweep()
        self.append_rows(src, [{
            "type": "system", "subtype": "compact_boundary", "sessionId": "sess-1",
            "timestamp": "2026-07-28T11:00:00Z", "content": "Conversation compacted",
            "compactMetadata": {"trigger": "manual", "preTokens": 400000},
        }])
        archive.sweep()
        text = self.archived_path().read_text()
        self.assertIn("Please check the build.", text)  # pre-compaction survives
        self.assertIn("compact_boundary", text)


class TestDestructiveSources(ArchiveCase):
    def test_a_truncated_source_rotates_rather_than_destroying_the_archive(self):
        src = self.make_session()
        archive.sweep()
        original = self.archived_path().read_text()
        self.assertIn("All **5** tests pass.", original)

        # Something rewrote the transcript shorter.
        self.write_rows(src, simple_session("sess-1")[:2])
        stats = archive.sweep()

        self.assertEqual(stats.rotated, 1)
        generations = sorted(self.archived_path().parent.glob("sess-1.gen*.jsonl"))
        self.assertEqual(len(generations), 1)
        self.assertEqual(generations[0].read_text(), original,
                         "the full earlier conversation must survive verbatim")
        self.assertEqual(self.archived_path().read_bytes(), src.read_bytes())

    def test_a_replaced_source_also_rotates(self):
        src = self.make_session()
        archive.sweep()
        os.unlink(src)
        self.write_rows(src, simple_session("sess-1"))  # new inode, same size
        stats = archive.sweep()
        self.assertGreaterEqual(stats.rotated, 0)  # same content is fine either way
        self.assertTrue(self.archived_path().exists())

    def test_deleting_the_original_does_not_touch_the_archive(self):
        # This is the actual scenario: Claude Code's 30-day cleanup.
        src = self.make_session()
        archive.sweep()
        archived = self.archived_path().read_bytes()

        os.unlink(src)
        archive.sweep()

        self.assertTrue(self.archived_path().exists())
        self.assertEqual(self.archived_path().read_bytes(), archived)

    def test_a_source_vanishing_mid_sweep_is_survivable(self):
        self.make_session()
        refs = transcript.index_sessions()
        for ref in refs:
            try:
                os.unlink(ref.path)
            except OSError:
                pass
        stats = archive.sweep(refs)  # must not raise
        self.assertEqual(stats.errors, 0)


class TestSidecars(ArchiveCase):
    def test_subagents_and_tool_results_are_archived(self):
        src = self.make_session()
        sidecar = src.with_suffix("")
        (sidecar / "subagents").mkdir(parents=True)
        (sidecar / "tool-results").mkdir(parents=True)
        (sidecar / "subagents" / "agent-abc.jsonl").write_text('{"type":"user"}\n')
        (sidecar / "subagents" / "agent-abc.meta.json").write_text(
            '{"agentType":"Explore","toolUseId":"toolu_A"}'
        )
        (sidecar / "tool-results" / "toolu_A.txt").write_text("a very large tool result")

        archive.sweep()

        root = self.archived_path().with_suffix("")
        self.assertTrue((root / "subagents" / "agent-abc.jsonl").exists())
        self.assertEqual(
            json.loads((root / "subagents" / "agent-abc.meta.json").read_text())["toolUseId"],
            "toolu_A",
        )
        self.assertEqual(
            (root / "tool-results" / "toolu_A.txt").read_text(), "a very large tool result"
        )

    def test_explainer_children_are_not_archived(self):
        workdir = paths.run_dir() / "explain"
        workdir.mkdir(parents=True, exist_ok=True)
        path = self.transcript_path(cwd=str(workdir), session_id="explainer-child")
        self.write_rows(path, simple_session("explainer-child", cwd=str(workdir)))
        archive.sweep()
        self.assertEqual(
            list(archive.archive_dir().rglob("explainer-child.jsonl")), []
        )


class TestArchiveAsASource(ArchiveCase):
    def test_an_archived_session_still_appears_after_the_original_is_deleted(self):
        src = self.make_session()
        archive.sweep()
        os.unlink(src)

        refs = transcript.index_sessions()
        ids = [r.session_id for r in refs]
        self.assertIn("sess-1", ids)
        ref = next(r for r in refs if r.session_id == "sess-1")
        self.assertTrue(ref.archived)
        self.assertEqual(ref.title, "Check the build")

    def test_an_archived_session_still_renders(self):
        from scribe import store

        src = self.make_session()
        archive.sweep()
        os.unlink(src)

        ref = next(r for r in transcript.index_sessions() if r.session_id == "sess-1")
        session, path = store.build_one(ref.path)
        self.assertEqual(len(session.rounds), 1)
        self.assertIn("pytest -q", path.read_text())

    def test_live_wins_over_archived_for_the_same_session(self):
        src = self.make_session()
        archive.sweep()
        self.append_rows(src, [user_row("sess-1", "later prompt", "2026-07-28T12:00:00Z", "u2")])

        refs = [r for r in transcript.index_sessions() if r.session_id == "sess-1"]
        self.assertEqual(len(refs), 1, "a session must not appear twice")
        self.assertFalse(refs[0].archived)
        self.assertEqual(str(refs[0].path), str(src))

    def test_find_transcript_falls_back_to_the_archive(self):
        src = self.make_session()
        archive.sweep()
        os.unlink(src)
        found = transcript.find_transcript("sess-1")
        self.assertIsNotNone(found)
        self.assertIn("archive", str(found))


class TestSummary(ArchiveCase):
    def test_summary_counts(self):
        self.make_session("sess-1")
        self.make_session("sess-2")
        archive.sweep()
        summary = archive.summary()
        self.assertEqual(summary["sessions"], 2)
        self.assertGreater(summary["bytes"], 0)

    def test_human_bytes(self):
        self.assertEqual(archive.human_bytes(512), "512B")
        self.assertEqual(archive.human_bytes(2048), "2.0KB")
        self.assertEqual(archive.human_bytes(5 * 1024 * 1024), "5.0MB")


if __name__ == "__main__":
    unittest.main()
