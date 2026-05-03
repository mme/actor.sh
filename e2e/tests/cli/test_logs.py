"""e2e: `actor logs` — claude session log rendering."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorLogsTests(unittest.TestCase):

    def test_logs_shows_response(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("the answer is 42"))
            r = env.run_cli(["logs", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("the answer is 42", r.stdout)

    def test_logs_verbose_includes_thinking(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("answer", thinking="my reasoning"))
            r = env.run_cli(["logs", "alice", "--verbose"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            # Verbose should expose thinking and timestamps.
            self.assertIn("my reasoning", r.stdout)

    def test_logs_verbose_includes_tool_calls(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds(
                "done",
                tools=[{"id": "t1", "name": "Bash", "input": {"cmd": "ls"},
                        "result": "file1\nfile2"}],
            ))
            r = env.run_cli(["logs", "alice", "--verbose"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("Bash", r.stdout)

    def test_logs_no_runs_message(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["logs", "alice"])
            # No runs yet — should be a friendly message, not a crash.
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
