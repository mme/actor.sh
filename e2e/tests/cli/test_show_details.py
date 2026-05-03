"""e2e: `actor show` content checks."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ShowDetailTests(unittest.TestCase):

    def test_show_includes_actor_name(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("alice", r.stdout)

    def test_show_includes_agent_kind(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertIn("claude", r.stdout.lower())

    def test_show_includes_dir_path(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            r = env.run_cli(["show", "alice"])
            self.assertIn(actor.dir, r.stdout)

    def test_show_includes_config_section(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            r = env.run_cli(["show", "alice"])
            self.assertIn("opus", r.stdout)

    def test_show_recent_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first task"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second task"], **claude_responds("b"))
            r = env.run_cli(["show", "alice"])
            self.assertIn("first task", r.stdout)
            self.assertIn("second task", r.stdout)


if __name__ == "__main__":
    unittest.main()
