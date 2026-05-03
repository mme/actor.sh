"""e2e: rapid create/discard cycles."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class QuickLifecycleTests(unittest.TestCase):

    def test_rapid_create_discard_loop(self):
        with isolated_home() as env:
            for i in range(5):
                r = env.run_cli(["new", f"a{i}", "--no-worktree"])
                self.assertEqual(r.returncode, 0, msg=r.stderr)
                r = env.run_cli(["discard", f"a{i}", "--force"])
                self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(env.list_actor_names(), [])

    def test_discard_preserves_branch_and_recreate_fails(self):
        # Documented contract: discard intentionally does NOT delete
        # the underlying git branch. The default on-discard hook
        # (`git diff --quiet`) only catches unstaged modifications,
        # so destroying the branch on discard would silently lose
        # committed work. Recovery for the recreate case is `git
        # branch -D <name>` in the source repo, or rename the new
        # actor.
        import subprocess
        with isolated_home() as env:
            r1 = env.run_cli(["new", "alice"])
            self.assertEqual(r1.returncode, 0, msg=r1.stderr)
            actor = env.fetch_actor("alice")
            source_repo = actor.source_repo
            r2 = env.run_cli(["discard", "alice"])
            self.assertEqual(r2.returncode, 0, msg=r2.stderr)
            # Branch survives the discard.
            branches = subprocess.run(
                ["git", "branch", "--list", "alice"],
                cwd=source_repo, capture_output=True, text=True,
            )
            self.assertIn("alice", branches.stdout)
            # Recreate fails honestly with "branch already exists".
            r3 = env.run_cli(["new", "alice"])
            self.assertNotEqual(r3.returncode, 0)
            self.assertIn("already exists", r3.stderr.lower())


if __name__ == "__main__":
    unittest.main()
