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
    _DEPLOY_BLOCK_BEGIN,
    _DEPLOY_BLOCK_END,
    _parse_deployed_version,
    _skill_target_dir,
    _stamp_deploy_block,
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


def _fake_skill_md(tmp: Path) -> Path:
    """A minimal SKILL.md with the deploy-block markers in place."""
    path = tmp / "SKILL.md"
    path.write_text(
        "---\nname: actor\n---\n\n# Actor\n\n"
        f"{_DEPLOY_BLOCK_BEGIN}\n"
        f"{_DEPLOY_BLOCK_END}\n\n"
        "body content\n"
    )
    return path


class StampDeployBlockTests(unittest.TestCase):
    def test_fills_empty_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _fake_skill_md(Path(tmp))
            _stamp_deploy_block(path, "1.2.3")
            out = path.read_text()
            self.assertIn("actor-sh 1.2.3", out)
            # Markers still there, exactly once each
            self.assertEqual(out.count(_DEPLOY_BLOCK_BEGIN), 1)
            self.assertEqual(out.count(_DEPLOY_BLOCK_END), 1)

    def test_replaces_existing_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _fake_skill_md(Path(tmp))
            _stamp_deploy_block(path, "1.0.0")
            _stamp_deploy_block(path, "2.0.0")
            out = path.read_text()
            self.assertIn("actor-sh 2.0.0", out)
            self.assertNotIn("actor-sh 1.0.0", out)
            # Body outside the block is preserved
            self.assertIn("body content", out)

    def test_missing_markers_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text("# hello\nno markers\n")
            with self.assertRaises(ActorError):
                _stamp_deploy_block(path, "1.0.0")


class DeployedVersionParseTests(unittest.TestCase):
    def test_extracts_version_from_block(self):
        block = (
            f"# Actor\n{_DEPLOY_BLOCK_BEGIN}\n"
            "This skill was deployed from **actor-sh 0.3.1**. …\n"
            f"{_DEPLOY_BLOCK_END}\n"
        )
        self.assertEqual(_parse_deployed_version(block), "0.3.1")

    def test_no_block_returns_none(self):
        self.assertIsNone(_parse_deployed_version("# no markers"))

    def test_empty_block_returns_none(self):
        text = f"{_DEPLOY_BLOCK_BEGIN}\n{_DEPLOY_BLOCK_END}\n"
        self.assertIsNone(_parse_deployed_version(text))

    def test_roundtrip_for_pep440_versions(self):
        """Stamp → parse → same version for realistic PEP 440 strings."""
        from actor.setup import _deploy_block
        for version in (
            "0.1.4",
            "0.1.4.dev3+g1a2b3c4",
            "0.1.dev190+gb95ac3fa7.d20260419",
            "1.0.0",
            "1.0.0rc1",
            "1.0.0+local.with.dot",
            "0.0.0-old",
        ):
            with self.subTest(version=version):
                block = _deploy_block(version)
                self.assertEqual(_parse_deployed_version(block), version)


class StampIdempotencyTests(unittest.TestCase):
    def test_double_stamp_same_version_is_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _fake_skill_md(Path(tmp))
            _stamp_deploy_block(path, "1.2.3")
            first = path.read_text()
            _stamp_deploy_block(path, "1.2.3")
            self.assertEqual(path.read_text(), first)

    def test_prefix_and_suffix_preserved(self):
        """Content outside the block must survive byte-for-byte."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "PREFIX_BEFORE_BLOCK\n"
                f"{_DEPLOY_BLOCK_BEGIN}\n{_DEPLOY_BLOCK_END}\n"
                "SUFFIX_AFTER_BLOCK\n"
            )
            _stamp_deploy_block(path, "1.0.0")
            out = path.read_text()
            self.assertTrue(out.startswith("PREFIX_BEFORE_BLOCK\n"))
            self.assertTrue(out.endswith("SUFFIX_AFTER_BLOCK\n"))


class SetupEndToEndTests(unittest.TestCase):
    def _fake_home(self):
        """Context manager that provides a tmp HOME and fakes 'claude mcp add'."""
        return tempfile.TemporaryDirectory()

    def test_setup_installs_and_is_idempotent(self):
        with self._fake_home() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add") as mcp_add, \
                 patch("actor.setup._claude_mcp_remove") as mcp_remove:
                msg = cmd_setup(for_host="claude-code", scope="user", name="actor")
                target = Path(tmp) / ".claude" / "skills" / "actor"
                self.assertTrue((target / "SKILL.md").exists())
                self.assertTrue((target / "cli.md").exists())
                self.assertIn("actor-sh ", (target / "SKILL.md").read_text())
                mcp_add.assert_called_once_with(name="actor", scope="user", for_host="claude-code")
                mcp_remove.assert_not_called()  # nothing to clobber on first install
                self.assertIn("installed", msg)

                # Re-run: idempotent — replaces skill and re-registers MCP
                mcp_add.reset_mock()
                cmd_setup(for_host="claude-code", scope="user", name="actor")
                self.assertTrue((target / "SKILL.md").exists())
                mcp_add.assert_called_once()
                mcp_remove.assert_called_once_with(name="actor", scope="user")

    def test_setup_rejects_unsupported_host(self):
        with self._fake_home() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            with self.assertRaises(ActorError):
                cmd_setup(for_host="cursor", scope="user", name="actor")


class UpdateEndToEndTests(unittest.TestCase):
    def test_update_refreshes_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add"):
                cmd_setup(for_host="claude-code", scope="user", name="actor")
                skill = Path(tmp) / ".claude" / "skills" / "actor" / "SKILL.md"

                # Simulate a stale deploy by rewriting the version string.
                text = skill.read_text()
                import re
                text = re.sub(r"\*\*actor-sh [^\*]+\*\*", "**actor-sh 0.0.0-old**", text)
                skill.write_text(text)

                msg = cmd_update(for_host="claude-code", scope="user", name="actor")
                self.assertIn("updated from 0.0.0-old", msg)

    def test_update_noop_when_same_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add"):
                cmd_setup(for_host="claude-code", scope="user", name="actor")
                msg = cmd_update(for_host="claude-code", scope="user", name="actor")
                self.assertIn("already at version", msg)

    def test_update_without_setup_errors(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"HOME": tmp}):
            with self.assertRaises(ActorError):
                cmd_update(for_host="claude-code", scope="user", name="actor")


class NameValidationTests(unittest.TestCase):
    def test_rejects_traversal(self):
        from actor.setup import _validate_name
        for bad in ("", ".", "..", "../evil", "a/b", ".hidden", "-start"):
            with self.subTest(bad=bad), self.assertRaises(ActorError):
                _validate_name(bad)

    def test_accepts_normal_names(self):
        from actor.setup import _validate_name
        for good in ("actor", "actor-dev", "actor_v2", "actor.beta", "a1"):
            _validate_name(good)  # no raise


class ClaudeMcpSubprocessTests(unittest.TestCase):
    def test_claude_missing_raises_actor_error(self):
        from actor.setup import _claude_mcp_add
        with patch("actor.setup.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(ActorError) as ctx:
                _claude_mcp_add(name="actor", scope="user", for_host="claude-code")
            self.assertIn("claude", str(ctx.exception).lower())

    def test_claude_timeout_raises_actor_error(self):
        import subprocess as _sp
        from actor.setup import _claude_mcp_add
        with patch(
            "actor.setup.subprocess.run",
            side_effect=_sp.TimeoutExpired(cmd=["claude"], timeout=30),
        ):
            with self.assertRaises(ActorError) as ctx:
                _claude_mcp_add(name="actor", scope="user", for_host="claude-code")
            self.assertIn("timed out", str(ctx.exception))

    def test_claude_nonzero_exit_surfaces_stderr(self):
        from unittest.mock import MagicMock
        from actor.setup import _claude_mcp_add
        fake_result = MagicMock(returncode=1, stderr="already exists", stdout="")
        with patch("actor.setup.subprocess.run", return_value=fake_result):
            with self.assertRaises(ActorError) as ctx:
                _claude_mcp_add(name="actor", scope="user", for_host="claude-code")
            self.assertIn("already exists", str(ctx.exception))


class BundledSkillTests(unittest.TestCase):
    def test_skips_python_artifacts(self):
        from actor.setup import _copy_bundled_skill
        with tempfile.TemporaryDirectory() as tmp:
            copied = _copy_bundled_skill(Path(tmp))
            for py_name in ("__init__.py", "__pycache__"):
                self.assertNotIn(py_name, copied)
            self.assertIn("SKILL.md", copied)


class SetupAtomicSwapTests(unittest.TestCase):
    """Verify --force keeps the old install intact when the new one fails mid-flight."""

    def test_post_swap_mcp_failure_mentions_retry(self):
        from actor.errors import ActorError
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add",
                       side_effect=ActorError("`claude mcp add` failed: nope")):
                with self.assertRaises(ActorError) as ctx:
                    cmd_setup(for_host="claude-code", scope="user", name="actor")
                self.assertIn("skill deployed to", str(ctx.exception))
                self.assertIn("actor setup", str(ctx.exception))
            target = Path(tmp) / ".claude" / "skills" / "actor"
            self.assertTrue((target / "SKILL.md").exists())

    def test_staging_failure_preserves_existing_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add"):
                # First install — should succeed and leave a working skill
                cmd_setup(for_host="claude-code", scope="user", name="actor")
                target = Path(tmp) / ".claude" / "skills" / "actor"
                first_text = (target / "SKILL.md").read_text()
                self.assertIn("actor-sh ", first_text)

            # Second install, but _copy_bundled_skill raises mid-way. The
            # existing install must survive.
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._copy_bundled_skill",
                       side_effect=ActorError("bundled skill resources are missing")), \
                 patch("actor.setup._claude_mcp_add"):
                with self.assertRaises(ActorError):
                    cmd_setup(for_host="claude-code", scope="user", name="actor")

            self.assertTrue((target / "SKILL.md").exists())
            self.assertEqual((target / "SKILL.md").read_text(), first_text)
            # No leftover staging directories
            stagings = list(target.parent.glob(f".actor-staging-*"))
            backups = list(target.parent.glob(f".actor-old-*"))
            self.assertEqual(stagings, [])
            self.assertEqual(backups, [])


class SetupVersionAssertionTests(unittest.TestCase):
    def test_stamped_version_matches_package_version(self):
        from actor import __version__
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}), \
                 patch("actor.setup._claude_mcp_add"):
                cmd_setup(for_host="claude-code", scope="user", name="actor")
                skill = Path(tmp) / ".claude" / "skills" / "actor" / "SKILL.md"
                self.assertEqual(_parse_deployed_version(skill.read_text()), __version__)


if __name__ == "__main__":
    unittest.main()
