"""e2e edge cases: long inputs, shell metas, unicode, malformed config, etc."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class EdgeCaseTests(unittest.TestCase):

    def test_unicode_in_prompt_passes_through(self):
        prompt = "résumé 日本語 emoji 🎉 done"
        with isolated_home() as env:
            env.run_cli(["new", "alice", prompt], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(invs[0]["parsed"]["prompt"], prompt)

    def test_prompt_with_shell_metas_passes_byte_exact(self):
        prompt = "echo $HOME `whoami` | grep x; rm -rf / # nope"
        with isolated_home() as env:
            env.run_cli(["new", "alice", prompt], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(invs[0]["parsed"]["prompt"], prompt)

    def test_long_prompt_under_argmax(self):
        prompt = "x" * 10_000
        with isolated_home() as env:
            env.run_cli(["new", "alice", prompt], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(len(invs[0]["parsed"]["prompt"]), 10_000)

    def test_invalid_actor_name_with_spaces_rejected(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "name with spaces"])
            self.assertNotEqual(r.returncode, 0)

    def test_malformed_settings_kdl_clear_error(self):
        with isolated_home() as env:
            env.write_settings_kdl("role \"qa\" { unclosed string")
            r = env.run_cli(["list"])
            # Either fail with a clear message or recover; never crash silently.
            if r.returncode != 0:
                self.assertIn("settings.kdl", r.stderr.lower() + r.stdout.lower())

    def test_zero_byte_stdin_friendly_error(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])  # create idle
            r = env.run_cli(["run", "alice"], input="")
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)

    def test_unknown_top_level_kdl_node_silently_ignored(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'alias "max" template="qa"\n'
                'role "qa" { agent "claude" }\n'
            )
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("qa", r.stdout)


if __name__ == "__main__":
    unittest.main()
