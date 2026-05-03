"""e2e: session resumption flag passing across multiple runs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class SessionResumptionTests(unittest.TestCase):

    def test_first_run_uses_session_id_not_resume(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            invs = env.claude_invocations()
            self.assertIsNotNone(invs[0]["parsed"]["session_id"])
            self.assertIsNone(invs[0]["parsed"]["resume"])

    def test_second_run_uses_resume_with_first_session_id(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            invs = env.claude_invocations()
            sid = invs[0]["parsed"]["session_id"]
            self.assertEqual(invs[1]["parsed"]["resume"], sid)
            # Second invocation should NOT pass --session-id (uses --resume).
            self.assertIsNone(invs[1]["parsed"]["session_id"])


if __name__ == "__main__":
    unittest.main()
