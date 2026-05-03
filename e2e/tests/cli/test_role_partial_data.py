"""e2e: roles with various partial data."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RolePartialDataTests(unittest.TestCase):

    def test_role_with_only_agent(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "minimal" {\n    agent "claude"\n}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "minimal"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_role_with_only_description(self):
        # No agent — defaults to claude per the docs.
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "doc-only" {\n    description "Just a description"\n}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "doc-only"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_role_with_only_prompt(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "prompt-only" {\n    prompt "system prompt"\n}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "prompt-only"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.agent_args.get("append-system-prompt"),
                "system prompt",
            )

    def test_role_with_just_config_keys(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "cfg-only" {\n'
                '    model "opus"\n'
                '    effort "max"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "cfg-only"])
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "opus")
            self.assertEqual(actor.config.agent_args.get("effort"), "max")


if __name__ == "__main__":
    unittest.main()
