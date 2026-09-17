"""Hook installation: the one thing that edits someone else's file."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path

from helpers import Isolated

from scribe import install, paths


class TestInstall(Isolated):
    def setUp(self):
        super().setUp()
        # install/uninstall print a short report for humans; it is noise here.
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.addCleanup(lambda: self._quiet.__exit__(None, None, None))

    def settings(self):
        return json.loads(install.settings_path().read_text())

    def test_installs_every_event(self):
        install.install()
        hooks = self.settings()["hooks"]
        self.assertEqual(set(hooks), set(install.EVENTS))
        self.assertTrue(install.is_installed())

    def test_permission_request_gets_a_long_timeout(self):
        # It is the one event we may legitimately hold while a human decides.
        install.install()
        entry = self.settings()["hooks"]["PermissionRequest"][0]["hooks"][0]
        self.assertGreaterEqual(entry["timeout"], 300)
        self.assertEqual(self.settings()["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"], 10)

    def test_tool_events_carry_an_empty_matcher(self):
        install.install()
        hooks = self.settings()["hooks"]
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "")
        self.assertNotIn("matcher", hooks["Stop"][0])

    def test_existing_hooks_are_preserved(self):
        target = install.settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "model": "opus",
            "hooks": {
                "PostToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "/usr/local/bin/my-linter"}]}],
            },
        }))
        install.install()
        settings = self.settings()
        self.assertEqual(settings["model"], "opus")
        commands = [
            h["command"]
            for group in settings["hooks"]["PostToolUse"]
            for h in group["hooks"]
        ]
        self.assertIn("/usr/local/bin/my-linter", commands)
        self.assertTrue(any("scribe-hook" in c for c in commands))

    def test_reinstall_does_not_duplicate(self):
        install.install()
        install.install()
        install.install()
        ours = [
            h
            for group in self.settings()["hooks"]["Stop"]
            for h in group["hooks"]
            if "scribe-hook" in h["command"]
        ]
        self.assertEqual(len(ours), 1)

    def test_uninstall_restores_the_original_exactly(self):
        target = install.settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        original = {
            "model": "opus",
            "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/usr/local/bin/my-linter"}]}]},
        }
        target.write_text(json.dumps(original))

        install.install()
        install.uninstall()
        self.assertEqual(self.settings(), original)
        self.assertFalse(install.is_installed())

    def test_uninstall_from_a_file_we_never_touched(self):
        target = install.settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"model": "opus"}))
        install.uninstall()
        self.assertEqual(self.settings(), {"model": "opus"})

    def test_a_backup_is_written(self):
        target = install.settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"model": "opus"}))
        install.install()
        backups = list(target.parent.glob("settings.json.scribe-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), {"model": "opus"})

    def test_writes_through_a_symlink_instead_of_replacing_it(self):
        # A dotfiles setup usually has ~/.claude/settings.json symlinked into a
        # repo. Replacing the link would silently detach the user's dotfiles.
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(json.dumps({"model": "opus"}))

        link = install.settings_path()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)

        install.install()
        self.assertTrue(link.is_symlink(), "the symlink must survive")
        self.assertEqual(link.resolve(), real.resolve())
        self.assertIn("hooks", json.loads(real.read_text()))

    def test_dry_run_writes_nothing(self):
        install.install(dry_run=True)
        self.assertFalse(install.settings_path().exists())

    def test_malformed_settings_do_not_destroy_the_file(self):
        target = install.settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ not json")
        install.install()
        # Unparseable input is treated as empty, but the backup keeps the original.
        backups = list(target.parent.glob("settings.json.scribe-backup-*"))
        self.assertEqual(backups[0].read_text(), "{ not json")
        self.assertTrue(install.is_installed())

    def test_the_command_points_at_a_real_executable(self):
        command = install.hook_command()
        path = command.split('" "')[-1].rstrip('"')
        self.assertTrue(Path(path).is_file(), path)
        self.assertIn("scribe-hook", command)


if __name__ == "__main__":
    unittest.main()


class TestFirstRun(Isolated):
    """Bare `scribe` is the whole setup: the first run registers the hooks."""

    def setUp(self):
        super().setUp()
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.addCleanup(lambda: self._quiet.__exit__(None, None, None))

    def test_the_first_run_installs_the_hooks_and_opens_the_page(self):
        from unittest import mock

        from scribe import cli, daemon

        opened = []
        with mock.patch.object(daemon, "ensure_running", return_value={"url": "http://127.0.0.1:1"}), \
                mock.patch.object(daemon.webbrowser, "open", opened.append):
            self.assertFalse(install.is_installed())
            self.assertEqual(cli.main([]), 0)
            self.assertTrue(install.is_installed())
            # The second run finds the hooks in place and only opens the page.
            before = install.settings_path().read_text()
            self.assertEqual(cli.main([]), 0)
            self.assertEqual(install.settings_path().read_text(), before)
        self.assertEqual(opened, ["http://127.0.0.1:1", "http://127.0.0.1:1"])
