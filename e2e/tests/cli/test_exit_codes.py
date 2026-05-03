"""e2e: exit codes for various conditions."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ExitCodeTests(unittest.TestCase):

    def test_new_already_exists_returns_nonzero(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["new", "alice"])
            self.assertNotEqual(r.returncode, 0)

    def test_run_unknown_actor_returns_nonzero(self):
        with isolated_home() as env:
            r = env.run_cli(["run", "ghost", "do x"])
            self.assertNotEqual(r.returncode, 0)

    def test_show_unknown_actor_returns_nonzero(self):
        with isolated_home() as env:
            r = env.run_cli(["show", "ghost"])
            self.assertNotEqual(r.returncode, 0)

    def test_discard_unknown_actor_returns_nonzero(self):
        with isolated_home() as env:
            r = env.run_cli(["discard", "ghost"])
            self.assertNotEqual(r.returncode, 0)

    def test_stop_unknown_actor_returns_nonzero(self):
        with isolated_home() as env:
            r = env.run_cli(["stop", "ghost"])
            self.assertNotEqual(r.returncode, 0)

    def test_logs_unknown_actor_returns_nonzero(self):
        with isolated_home() as env:
            r = env.run_cli(["logs", "ghost"])
            self.assertNotEqual(r.returncode, 0)

    def test_config_unknown_actor_returns_nonzero(self):
        with isolated_home() as env:
            r = env.run_cli(["config", "ghost"])
            self.assertNotEqual(r.returncode, 0)

    def test_discard_unknown_returns_friendly_message(self):
        with isolated_home() as env:
            r = env.run_cli(["discard", "ghost"])
            self.assertIn("ghost", r.stderr + r.stdout)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
