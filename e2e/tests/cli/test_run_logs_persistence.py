"""e2e: logs survive across runs and reflect history."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class LogsPersistenceTests(unittest.TestCase):

    def test_logs_show_latest_run_response(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("ALPHA"))
            env.run_cli(["run", "alice", "second"], **claude_responds("BETA"))
            r = env.run_cli(["logs", "alice"])
            # Logs follow the entire session — both responses should be there.
            self.assertIn("BETA", r.stdout)

    def test_logs_show_first_run_response_too(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("ALPHA"))
            env.run_cli(["run", "alice", "second"], **claude_responds("BETA"))
            r = env.run_cli(["logs", "alice"])
            # Sessions accumulate, so the original ALPHA response stays visible.
            self.assertIn("ALPHA", r.stdout)

    def test_logs_for_nonexistent_actor_errors_friendly(self):
        with isolated_home() as env:
            r = env.run_cli(["logs", "ghost"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
