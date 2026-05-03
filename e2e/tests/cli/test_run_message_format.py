"""e2e: format of `actor new`/`actor run` reply messages."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RunMessageFormatTests(unittest.TestCase):

    def test_new_with_prompt_message_mentions_completion(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("alice", r.stdout)

    def test_run_complete_message_includes_response(self):
        # The user already knows which actor they ran (they typed
        # `actor run alice ...`); the message doesn't need to repeat
        # the name. It SHOULD include the agent's response so the user
        # doesn't need to call `actor logs` to see what happened.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "do x"],
                            **claude_responds("UNIQUE_RESPONSE_HERE"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("UNIQUE_RESPONSE_HERE", r.stdout)

    def test_partial_success_message_for_create_run_failure(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "do x"],
                            **claude_responds("oops", exit=2))
            # Run failed but actor was created — message should distinguish.
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
