"""e2e: invariants on role application semantics."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RoleInvariantTests(unittest.TestCase):

    def test_role_prompt_carries_via_actor_run_too(self):
        # After applying a role at create time, the prompt should still
        # be passed via --append-system-prompt on subsequent runs (it's
        # snapshotted in the actor's config).
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"], "you are qa"
            )

    def test_role_with_no_prompt_does_not_set_system_prompt(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    model "opus"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa", "do x"],
                        **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertIsNone(invs[0]["parsed"]["append_system_prompt"])

    def test_role_with_explicit_append_system_prompt_in_config_wins(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "from prompt field"\n'
                '    append-system-prompt "from config key"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa", "do x"],
                        **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"], "from config key"
            )


if __name__ == "__main__":
    unittest.main()
