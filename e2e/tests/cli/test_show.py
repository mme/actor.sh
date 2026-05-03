"""e2e: `actor show` — actor details + recent runs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorShowTests(unittest.TestCase):

    def test_show_existing_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("alice", r.stdout)

    def test_show_runs_limit(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            env.run_cli(["run", "alice", "third"], **claude_responds("c"))
            r = env.run_cli(["show", "alice", "--runs", "2"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_show_runs_zero(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice", "--runs", "0"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_show_missing_actor(self):
        with isolated_home() as env:
            r = env.run_cli(["show", "ghost"])
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
