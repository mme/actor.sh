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

    def test_create_discard_create_with_worktree(self):
        # Verifies the discard-doesn't-clean-branch bug from a different angle.
        with isolated_home() as env:
            r1 = env.run_cli(["new", "alice"])
            self.assertEqual(r1.returncode, 0, msg=r1.stderr)
            r2 = env.run_cli(["discard", "alice"])
            self.assertEqual(r2.returncode, 0, msg=r2.stderr)
            r3 = env.run_cli(["new", "alice"])
            # If discard cleans the branch, this works. Otherwise:
            # branch already exists.
            self.assertEqual(r3.returncode, 0, msg=r3.stderr)


if __name__ == "__main__":
    unittest.main()
