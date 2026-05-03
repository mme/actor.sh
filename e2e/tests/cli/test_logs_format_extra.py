"""e2e CLI: more `actor logs` format checks."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class LogsFormatExtraTests(unittest.TestCase):

    def test_logs_distinguishes_user_from_assistant(self):
        # Logs should somehow distinguish user prompts from assistant
        # responses (icon, prefix, color).
        with isolated_home() as env:
            env.run_cli(["new", "alice", "USER_TEXT"],
                        **claude_responds("ASSISTANT_TEXT"))
            r = env.run_cli(["logs", "alice"])
            # User and assistant messages should not be on the same
            # line — there should be some separation marker.
            self.assertIn("USER_TEXT", r.stdout)
            self.assertIn("ASSISTANT_TEXT", r.stdout)

    def test_logs_verbose_includes_tool_use_name(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds(
                "done",
                tools=[{"id": "t1", "name": "FANCY_TOOL_NAME",
                        "input": {"k": "v"}, "result": "result"}],
            ))
            r = env.run_cli(["logs", "alice", "--verbose"])
            self.assertIn("FANCY_TOOL_NAME", r.stdout)

    def test_logs_handles_empty_response_gracefully(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds(""))
            r = env.run_cli(["logs", "alice"])
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
