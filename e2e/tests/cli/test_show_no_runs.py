"""e2e: edge case — `actor show` for an actor with zero runs."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class ShowNoRunsTests(unittest.TestCase):

    def test_show_idle_actor_with_no_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_show_idle_actor_runs_zero(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice", "--runs", "0"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
