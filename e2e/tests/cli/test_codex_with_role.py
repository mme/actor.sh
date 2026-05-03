"""e2e: codex agent with role variations."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import codex_responds
from e2e.harness.isolated_home import isolated_home


class CodexWithRoleTests(unittest.TestCase):

    def test_codex_role_with_no_prompt_works(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "code" {\n'
                '    agent "codex"\n'
                '    m "o3"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "code"],
                            **codex_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("m"), "o3")

    def test_codex_role_with_prompt_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "codex"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("codex", r.stderr.lower())

    def test_cli_agent_codex_overrides_claude_role(self):
        # Role says claude with prompt; CLI overrides to codex.
        # Should error because resulting codex actor would have a prompt.
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa",
                             "--agent", "codex"])
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
