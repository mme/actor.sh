"""e2e CLI: one more probe."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class OneMoreTests(unittest.TestCase):

    def test_show_for_codex_actor_includes_codex_in_details(self):
        # If actor's agent is codex, that should be visible in show.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--agent", "codex"])
            r = env.run_cli(["show", "alice"])
            # Strict: should specifically say "codex" not just generic "agent".
            self.assertIn("codex", r.stdout.lower())

    def test_logs_show_user_and_assistant_visually_distinct(self):
        # Logs should somehow distinguish user / assistant turns.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "USER_MESSAGE"],
                        **claude_responds("ASSISTANT_MESSAGE"))
            r = env.run_cli(["logs", "alice"])
            # Find both, then check they're on different lines.
            user_line = -1
            asst_line = -1
            for i, line in enumerate(r.stdout.splitlines()):
                if "USER_MESSAGE" in line:
                    user_line = i
                if "ASSISTANT_MESSAGE" in line:
                    asst_line = i
            self.assertNotEqual(user_line, -1, "USER_MESSAGE not in logs")
            self.assertNotEqual(asst_line, -1, "ASSISTANT_MESSAGE not in logs")
            self.assertNotEqual(user_line, asst_line,
                                "user + assistant should be on separate lines")


if __name__ == "__main__":
    unittest.main()
