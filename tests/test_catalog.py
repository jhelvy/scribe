"""The slash catalogue: skills and commands from disk and from Claude."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from helpers import Isolated

from scribe import catalog, paths


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestFrontmatter(unittest.TestCase):
    def test_plain_quoted_and_folded(self):
        text = '---\nname: ph-app\ndescription: "Ship a Mac app: build, sign, notarize"\nargument-hint: <version>\nlong: >\n  first line\n  second line\nuser-invocable: false\n---\n# body\n'
        meta = catalog.frontmatter(text)
        self.assertEqual(meta["name"], "ph-app")
        self.assertEqual(meta["description"], "Ship a Mac app: build, sign, notarize")
        self.assertEqual(meta["argument-hint"], "<version>")
        self.assertEqual(meta["long"], "first line second line")
        self.assertEqual(meta["user-invocable"], "false")

    def test_no_front_matter(self):
        self.assertEqual(catalog.frontmatter("# just a file\n"), {})
        self.assertEqual(catalog.frontmatter("---\nunterminated: yes\n"), {})


class TestScan(Isolated):
    def setUp(self):
        super().setUp()
        self.home = paths.claude_home()
        self.proj = self.tmp / "proj"
        write(self.home / "skills" / "ph-app" / "SKILL.md", "---\nname: ph-app\ndescription: Mac apps\n---\n")
        write(self.home / "skills" / "secret" / "SKILL.md", "---\nname: secret\ndescription: hidden\nuser-invocable: false\n---\n")
        write(self.home / "skills" / "noname" / "SKILL.md", "---\ndescription: named by its folder\n---\n")
        write(self.home / "commands" / "deploy.md", "---\ndescription: Deploy it\nargument-hint: <env>\n---\nDeploy $ARGUMENTS")
        write(self.home / "commands" / "frontend" / "component.md", "Make a component")
        write(self.proj / ".claude" / "skills" / "local-skill" / "SKILL.md", "---\nname: local-skill\ndescription: only here\n---\n")
        write(self.proj / ".claude" / "commands" / "ph-app.md", "---\ndescription: shadows the user skill\n---\n")
        plug = self.home / "plugins" / "cache" / "market" / "codex" / "1.0.0"
        write(plug / "commands" / "review.md", "---\ndescription: Review with Codex\n---\n")
        write(plug / "skills" / "internal" / "SKILL.md", "---\nname: internal\nuser-invocable: false\n---\n")
        off = self.home / "plugins" / "cache" / "market" / "off" / "1.0.0"
        write(off / "commands" / "nope.md", "---\ndescription: disabled plugin\n---\n")
        write(
            self.home / "plugins" / "installed_plugins.json",
            json.dumps({"version": 2, "plugins": {
                "codex@market": [{"installPath": str(plug)}],
                "off@market": [{"installPath": str(off)}],
                "gone@market": [{"installPath": str(self.home / "nowhere")}],
            }}),
        )
        write(self.home / "settings.json", json.dumps({"enabledPlugins": {"codex@market": True, "off@market": False}}))

    def names(self, entries):
        return {e["name"]: e for e in entries}

    def test_every_source_is_found_and_namespaced(self):
        found = self.names(catalog.scan(str(self.proj)))
        self.assertIn("ph-app", found)
        self.assertIn("noname", found)
        self.assertEqual(found["deploy"]["argument_hint"], "<env>")
        self.assertEqual(found["deploy"]["kind"], "command")
        self.assertIn("frontend:component", found)
        self.assertEqual(found["local-skill"]["scope"], "project")
        self.assertEqual(found["codex:review"]["scope"], "plugin")
        self.assertEqual(found["codex:review"]["description"], "Review with Codex")

    def test_hidden_disabled_and_missing_are_left_out(self):
        found = self.names(catalog.scan(str(self.proj)))
        self.assertNotIn("secret", found)
        self.assertNotIn("codex:internal", found)
        self.assertNotIn("off:nope", found)

    def test_first_source_wins_a_name_clash(self):
        found = self.names(catalog.scan(str(self.proj)))
        self.assertEqual(found["ph-app"]["scope"], "user")
        self.assertEqual(found["ph-app"]["description"], "Mac apps")

    def test_no_cwd_means_no_project_entries(self):
        found = self.names(catalog.scan(""))
        self.assertNotIn("local-skill", found)
        self.assertIn("ph-app", found)


class TestMerge(Isolated):
    LIVE = {
        "commands": [
            {"name": "ph-app", "description": "Mac apps (user)", "argument_hint": ""},
            {"name": "simplify", "description": "Simplify the changed code", "argument_hint": ""},
            {"name": "compact", "description": "Compact", "argument_hint": "[focus]"},
            {"name": "doctor", "description": "Diagnose", "argument_hint": ""},
            {"name": "__internal", "description": "", "argument_hint": ""},
        ],
        "skills": ["ph-app", "simplify"],
        "terminal_commands": ["doctor"],
    }

    def test_claude_adds_bundled_skills_and_builtins(self):
        disk = [{"name": "ph-app", "description": "Mac apps", "argument_hint": "", "scope": "user", "kind": "skill", "terminal_only": False}]
        merged = {e["name"]: e for e in catalog.merge(disk, catalog.from_claude(self.LIVE))}
        self.assertEqual(merged["ph-app"]["scope"], "user")
        self.assertEqual(merged["ph-app"]["description"], "Mac apps")
        self.assertEqual(merged["simplify"]["kind"], "skill")
        self.assertEqual(merged["simplify"]["scope"], "claude")
        self.assertEqual(merged["compact"]["kind"], "builtin")
        self.assertTrue(merged["doctor"]["terminal_only"])
        self.assertNotIn("__internal", merged)

    def test_without_claude_a_fallback_list_fills_in(self):
        merged = {e["name"]: e for e in catalog.merge([], [])}
        self.assertIn("compact", merged)
        self.assertEqual(merged["compact"]["kind"], "builtin")

    def test_remembered_answers_survive(self):
        catalog.remember(self.LIVE)
        entries, source = catalog.catalogue("", None)
        self.assertEqual(source, "cache")
        self.assertIn("simplify", {e["name"] for e in entries})
        entries, source = catalog.catalogue("", self.LIVE)
        self.assertEqual(source, "live")
        os.remove(catalog.cache_file())
        entries, source = catalog.catalogue("", None)
        self.assertEqual(source, "disk")

    def test_availability_per_channel(self):
        skill = {"kind": "skill", "terminal_only": False}
        builtin = {"kind": "builtin", "terminal_only": False}
        tui = {"kind": "builtin", "terminal_only": True}
        self.assertTrue(catalog.available(skill, "driver")[0])
        self.assertTrue(catalog.available(builtin, "spawn")[0])
        self.assertTrue(catalog.available(skill, "inbox")[0])
        self.assertFalse(catalog.available(builtin, "inbox")[0])
        self.assertFalse(catalog.available(tui, "driver")[0])
        self.assertFalse(catalog.available(skill, "")[0])


class TestFiles(Isolated):
    def test_walk_skips_bulk_and_ranks_basename_matches_first(self):
        root = self.tmp / "repo"
        for rel in ("src/app.js", "src/main.py", "README.md", "node_modules/x/app.js", ".git/config", "docs/apple.md"):
            write(root / rel, "x")
        got = [f["path"] for f in catalog.list_files(str(root), "app")]
        self.assertEqual(got[0], "src/app.js")
        self.assertIn("docs/apple.md", got)
        self.assertNotIn("node_modules/x/app.js", got)
        self.assertNotIn(".git/config", got)
        self.assertEqual(catalog.list_files(str(root / "missing")), [])


if __name__ == "__main__":
    unittest.main()
