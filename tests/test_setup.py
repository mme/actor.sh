"""Tests for the setup/update commands (src/actor/setup.py)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actor.errors import ActorError
from actor.setup import (
    cmd_setup,
    cmd_update,
    _parse_frontmatter_version,
    _skill_target_dir,
    _stamp_version,
)


class TargetResolutionTests(unittest.TestCase):
    def test_user_scope_uses_home_dir(self):
        with patch.dict(os.environ, {"HOME": "/home/alice"}):
            target = _skill_target_dir("claude-code", "user", "actor")
            self.assertEqual(str(target), "/home/alice/.claude/skills/actor")

    def test_project_scope_uses_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("actor.setup.Path.cwd", return_value=Path(tmp)):
                target = _skill_target_dir("claude-code", "project", "actor")
                self.assertEqual(target, Path(tmp) / ".claude" / "skills" / "actor")

    def test_custom_name(self):
        with patch.dict(os.environ, {"HOME": "/home/alice"}):
            target = _skill_target_dir("claude-code", "user", "actor-dev")
            self.assertEqual(target.name, "actor-dev")

    def test_unknown_host_errors(self):
        with self.assertRaises(ActorError):
            _skill_target_dir("cursor", "user", "actor")


class StampVersionTests(unittest.TestCase):
    def test_inserts_when_absent(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
            f.write("---\nname: actor\ndescription: x\n---\n\nbody\n")
            path = Path(f.name)
        try:
            _stamp_version(path, "1.2.3")
            out = path.read_text()
            self.assertIn("version: 1.2.3", out)
            self.assertEqual(out.count("version:"), 1)
        finally:
            path.unlink()

    def test_replaces_existing(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
            f.write("---\nname: actor\nversion: 0.1.0\ndescription: x\n---\n\nbody\n")
            path = Path(f.name)
        try:
            _stamp_version(path, "2.0.0")
            out = path.read_text()
            self.assertIn("version: 2.0.0", out)
            self.assertNotIn("version: 0.1.0", out)
            self.assertEqual(out.count("version:"), 1)
        finally:
            path.unlink()

    def test_missing_frontmatter_raises(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
            f.write("# hello\nno frontmatter\n")
            path = Path(f.name)
        try:
            with self.assertRaises(ActorError):
                _stamp_version(path, "1.0.0")
        finally:
            path.unlink()


class FrontmatterParseTests(unittest.TestCase):
    def test_extracts_version(self):
        text = "---\nname: actor\nversion: 0.3.1\n---\nbody\n"
        self.assertEqual(_parse_frontmatter_version(text), "0.3.1")

    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(_parse_frontmatter_version("# no frontmatter"))

    def test_no_version_key_returns_none(self):
        text = "---\nname: actor\n---\nbody\n"
        self.assertIsNone(_parse_frontmatter_version(text))


class SetupEndToEndTests(unittest.TestCase):
    def _fake_home(self):
        """Context manager that provides a tmp HOME and fakes 'claude mcp add'."""
        return tempfile.TemporaryDirectory()

    def test_setup_installs_and_errors_on_existing(self):
        with self._fake_home() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add") as mcp_add:
                msg = cmd_setup(for_host="claude-code", scope="user", name="actor", force=False)
                target = Path(tmp) / ".claude" / "skills" / "actor"
                self.assertTrue((target / "SKILL.md").exists())
                self.assertTrue((target / "cli.md").exists())
                self.assertIn("version: ", (target / "SKILL.md").read_text())
                mcp_add.assert_called_once_with(name="actor", scope="user", for_host="claude-code")
                self.assertIn("installed", msg)

                # Re-run without --force: errors
                with self.assertRaises(ActorError):
                    cmd_setup(for_host="claude-code", scope="user", name="actor", force=False)

                # Re-run with --force: succeeds (idempotent overwrite)
                cmd_setup(for_host="claude-code", scope="user", name="actor", force=True)
                self.assertTrue((target / "SKILL.md").exists())

    def test_setup_rejects_unsupported_host(self):
        with self._fake_home() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            with self.assertRaises(ActorError):
                cmd_setup(for_host="cursor", scope="user", name="actor", force=False)


class UpdateEndToEndTests(unittest.TestCase):
    def test_update_refreshes_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add"):
                cmd_setup(for_host="claude-code", scope="user", name="actor", force=False)
                target = Path(tmp) / ".claude" / "skills" / "actor"

                # Simulate an older version by overwriting the stamp
                skill = target / "SKILL.md"
                text = skill.read_text()
                text = text.replace("version: ", "version: 0.0.0-old\n# ", 1)
                # The replace above is hacky; just rewrite cleanly
                lines = skill.read_text().splitlines(keepends=True)
                for i, line in enumerate(lines):
                    if line.startswith("version:"):
                        lines[i] = "version: 0.0.0-old\n"
                skill.write_text("".join(lines))

                msg = cmd_update(for_host="claude-code", scope="user", name="actor")
                self.assertIn("updated from 0.0.0-old", msg)

    def test_update_without_setup_errors(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            with self.assertRaises(ActorError):
                cmd_update(for_host="claude-code", scope="user", name="actor")


if __name__ == "__main__":
    unittest.main()
