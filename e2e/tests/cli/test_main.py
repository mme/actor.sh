"""e2e: `actor main` — orchestrator entrypoint that execs claude."""
from __future__ import annotations

import os
import subprocess
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorMainTests(unittest.TestCase):

    def test_actor_main_execs_claude_with_channel_and_prompt(self):
        # `actor main` execvp's into claude. The fake claude records its
        # invocation; verify the channel flag + append-system-prompt are
        # present.
        with isolated_home() as env:
            r = subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("orchestrator started", exit=0)),
                cwd=str(env.cwd),
                capture_output=True, text=True,
                timeout=10,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 1)
            parsed = invs[0]["parsed"]
            self.assertTrue(parsed["channel_flag"])
            self.assertIsNotNone(parsed["append_system_prompt"])
            self.assertIn("main actor", parsed["append_system_prompt"])

    def test_actor_main_overridden_role_uses_custom_prompt(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "main" {\n'
                '    agent "claude"\n'
                '    prompt "you are a custom orchestrator"\n'
                '}\n'
            )
            r = subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("ok", exit=0)),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"],
                "you are a custom orchestrator",
            )

    def test_actor_main_with_codex_role_errors(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "main" {\n'
                '    agent "codex"\n'
                '    prompt "codex orchestrator"\n'
                '}\n'
            )
            r = subprocess.run(
                ["actor", "main"],
                env=env.env(),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("codex", r.stderr.lower())

    def test_actor_main_forwards_trailing_args(self):
        with isolated_home() as env:
            r = subprocess.run(
                ["actor", "main", "--model", "opus", "kick off"],
                env=env.env(**claude_responds("ok", exit=0)),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            argv = invs[0]["argv"]
            self.assertIn("--model", argv)
            self.assertIn("opus", argv)
            self.assertIn("kick off", argv)


if __name__ == "__main__":
    unittest.main()
