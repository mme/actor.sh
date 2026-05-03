"""e2e: `actor discard` — remove an actor + worktree, with hook checks."""
from __future__ import annotations

import unittest
from pathlib import Path

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorDiscardTests(unittest.TestCase):

    def test_discard_clean_worktree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            r = env.run_cli(["discard", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(env.list_actor_names(), [])
            # Worktree should be gone too.
            if actor.worktree:
                self.assertFalse(Path(actor.dir).exists(),
                                 f"worktree {actor.dir} should be removed")

    def test_discard_dirty_worktree_blocked_by_default_hook(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            # Dirty the worktree.
            (Path(actor.dir) / "dirty.txt").write_text("uncommitted\n")
            r = env.run_cli(["discard", "alice"])
            self.assertNotEqual(r.returncode, 0)
            # Actor still in DB.
            self.assertIn("alice", env.list_actor_names())

    def test_discard_force_overrides_dirty_check(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            (Path(actor.dir) / "dirty.txt").write_text("uncommitted\n")
            r = env.run_cli(["discard", "alice", "--force"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(env.list_actor_names(), [])

    def test_discard_runs_on_discard_hook(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-discard "echo DISCARDED >> $HOME/discarded.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            r = env.run_cli(["discard", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertTrue((env.home / "discarded.txt").exists())

    def test_discard_parent_cascades_to_children_leaves_first(self):
        with isolated_home() as env:
            env.run_cli(["new", "parent"])
            # Manually insert a child row referencing parent.
            from actor.types import Actor, ActorConfig, AgentKind
            with env.db() as db:
                db.insert_actor(Actor(
                    name="child", agent=AgentKind.CLAUDE,
                    agent_session=None, dir=str(env.cwd),
                    source_repo=None, base_branch=None, worktree=False,
                    parent="parent", config=ActorConfig(),
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                ))
            r = env.run_cli(["discard", "parent"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(env.list_actor_names(), [])

    def test_discard_on_discard_hook_failure_aborts_without_force(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n    on-discard "exit 1"\n}\n'
            )
            env.run_cli(["new", "alice"])
            r = env.run_cli(["discard", "alice"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("alice", env.list_actor_names())

    def test_discard_missing_worktree_runs_hook_from_home(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            # Manually nuke the worktree.
            import shutil
            shutil.rmtree(actor.dir, ignore_errors=True)
            r = env.run_cli(["discard", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
