"""e2e: actor run with special / edge-case prompt inputs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RunSpecialInputsTests(unittest.TestCase):

    def test_run_with_only_whitespace_prompt(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice"], input="   \n   \n",
                            **claude_responds("ok"))
            # After stripping, prompt is empty — should be a friendly error.
            self.assertNotEqual(r.returncode, 0)

    def test_run_with_multiline_prompt(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "line one\nline two"],
                            **claude_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertIn("line one", invs[-1]["parsed"]["prompt"])
            self.assertIn("line two", invs[-1]["parsed"]["prompt"])

    def test_run_with_quoted_prompt_preserves_quotes(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            prompt = 'said "hello" with quotes'
            env.run_cli(["run", "alice", prompt], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(invs[-1]["parsed"]["prompt"], prompt)

    def test_run_with_unicode_prompt(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            prompt = "résumé 日本語 🎉"
            env.run_cli(["run", "alice", prompt], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(invs[-1]["parsed"]["prompt"], prompt)


if __name__ == "__main__":
    unittest.main()
