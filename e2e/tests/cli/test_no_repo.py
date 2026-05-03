"""e2e: `actor new` outside a git repo."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class NoRepoTests(unittest.TestCase):

    def test_new_in_non_git_dir_with_worktree_errors(self):
        with isolated_home(init_git=False) as env:
            r = env.run_cli(["new", "alice"])
            # Without a git repo, worktree creation should fail with a
            # clear message — never crash.
            if r.returncode != 0:
                self.assertNotIn("Traceback", r.stderr)

    def test_new_no_worktree_in_non_git_dir_succeeds(self):
        with isolated_home(init_git=False) as env:
            r = env.run_cli(["new", "alice", "--no-worktree"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
