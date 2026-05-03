"""e2e: aggressive tests targeting subtle bugs."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class AggressiveTests(unittest.TestCase):

    def test_multiple_config_flags_same_key_last_wins(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice",
                         "--config", "model=opus",
                         "--config", "model=haiku"])
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "haiku")

    def test_update_without_setup_friendly_error(self):
        with isolated_home() as env:
            r = env.run_cli(["update"])
            # Update when nothing installed should be a clear error or no-op.
            self.assertNotIn("Traceback", r.stderr)

    def test_uninstall_without_install_is_idempotent(self):
        with isolated_home() as env:
            r = env.run_cli([
                "setup", "--for", "claude-code", "--uninstall",
            ])
            self.assertNotIn("Traceback", r.stderr)

    def test_run_with_minus_minus_separator(self):
        # Some CLIs need -- to separate flags from positional. Check
        # that prompts that look like flags work.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "--", "--looks-like-a-flag"],
                            **claude_responds("ok"))
            self.assertNotIn("Traceback", r.stderr)

    def test_role_with_kdl_value_types(self):
        # Booleans, numbers, strings — all should coerce to string.
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    use-subscription false\n'
                '    max-budget-usd 5\n'
                '    temperature 0.5\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.actor_keys.get("use-subscription"), "false"
            )
            self.assertEqual(
                actor.config.agent_args.get("max-budget-usd"), "5"
            )

    def test_actor_new_when_worktree_dir_exists(self):
        # Pre-create a directory at the worktree path to force a collision.
        with isolated_home() as env:
            wt_path = env.home / ".actor" / "worktrees" / "alice"
            wt_path.mkdir(parents=True)
            (wt_path / "preexisting.txt").write_text("conflict")
            r = env.run_cli(["new", "alice"])
            # Should fail cleanly; no traceback.
            self.assertNotIn("Traceback", r.stderr)

    def test_new_then_immediate_show_works(self):
        # No race between create and show.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_multiple_actors_distinct_session_ids(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("a"))
            env.run_cli(["new", "bob", "do y"], **claude_responds("b"))
            actors = [env.fetch_actor(n) for n in ("alice", "bob")]
            self.assertNotEqual(
                actors[0].agent_session, actors[1].agent_session,
                "two actors must have different session ids",
            )

    def test_config_view_with_no_keys(self):
        # An actor created with no --config flag still has class-level
        # defaults applied. View should show them, not be empty.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["config", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            # Class default permission-mode=auto for claude.
            self.assertIn("permission-mode", r.stdout)


if __name__ == "__main__":
    unittest.main()
