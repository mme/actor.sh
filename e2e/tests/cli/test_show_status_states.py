"""e2e: `actor show` reports correct status for each state."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ShowStatusStatesTests(unittest.TestCase):

    def test_show_idle_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertIn("idle", r.stdout.lower())

    def test_show_done_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            self.assertIn("done", r.stdout.lower())

    def test_show_error_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("oops", exit=2))
            r = env.run_cli(["show", "alice"])
            self.assertIn("error", r.stdout.lower())


if __name__ == "__main__":
    unittest.main()
