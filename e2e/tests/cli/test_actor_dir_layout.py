"""e2e: actor.sh directory layout invariants."""
from __future__ import annotations

import unittest
from pathlib import Path

from e2e.harness.isolated_home import isolated_home


class DirLayoutTests(unittest.TestCase):

    def test_actor_db_lives_in_dot_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            self.assertTrue((env.home / ".actor" / "actor.db").is_file())

    def test_worktree_lives_under_dot_actor_worktrees(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertTrue(actor.dir.startswith(
                str(env.home / ".actor" / "worktrees")
            ))

    def test_worktree_dir_has_git_dir_marker(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            # Worktrees have a `.git` file (not directory) pointing to
            # the parent repo's gitdir.
            git_marker = Path(actor.dir) / ".git"
            self.assertTrue(git_marker.exists())

    def test_two_actors_have_different_worktree_dirs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            self.assertNotEqual(
                env.fetch_actor("alice").dir,
                env.fetch_actor("bob").dir,
            )


if __name__ == "__main__":
    unittest.main()
