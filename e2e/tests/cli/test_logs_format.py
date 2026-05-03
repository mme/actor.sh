"""e2e: actor logs output format details."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class LogsFormatTests(unittest.TestCase):

    def test_logs_shows_user_prompt(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "MY_USER_PROMPT_MARKER"],
                        **claude_responds("ok"))
            r = env.run_cli(["logs", "alice"])
            self.assertIn("MY_USER_PROMPT_MARKER", r.stdout)

    def test_logs_shows_assistant_response(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("ASSISTANT_REPLY_MARKER"))
            r = env.run_cli(["logs", "alice"])
            self.assertIn("ASSISTANT_REPLY_MARKER", r.stdout)

    def test_logs_verbose_includes_timestamp(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["logs", "alice", "--verbose"])
            # Verbose mode should include timestamps; the fake writes
            # ISO 8601 Z-suffixed strings.
            import re
            has_iso = bool(re.search(r"\d{4}-\d{2}-\d{2}", r.stdout))
            self.assertTrue(has_iso, "verbose logs should include timestamps")


if __name__ == "__main__":
    unittest.main()
