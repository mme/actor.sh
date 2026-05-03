"""e2e: operations on discarded / nonexistent actors."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RunAfterDiscardTests(unittest.TestCase):

    def test_run_after_discard_errors_friendly(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["discard", "alice", "--force"])
            r = env.run_cli(["run", "alice", "do x"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)

    def test_show_after_discard_errors_friendly(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["discard", "alice", "--force"])
            r = env.run_cli(["show", "alice"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)

    def test_logs_after_discard_errors_friendly(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["discard", "alice", "--force"])
            r = env.run_cli(["logs", "alice"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)

    def test_config_after_discard_errors_friendly(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["discard", "alice", "--force"])
            r = env.run_cli(["config", "alice"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)

    def test_double_discard_errors_friendly(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["discard", "alice", "--force"])
            r = env.run_cli(["discard", "alice", "--force"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
