"""e2e workflow: defining a role + applying it via CLI / MCP."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class RoleApplicationTests(unittest.TestCase):

    def test_role_prompt_becomes_append_system_prompt(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "reviewer" {\n'
                '    description "Code review"\n'
                '    agent "claude"\n'
                '    prompt "You are a senior code reviewer."\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "reviewer",
                         "review src/"], **claude_responds("done"))
            invs = env.claude_invocations()
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"],
                "You are a senior code reviewer.",
            )
            self.assertEqual(invs[0]["parsed"]["prompt"], "review src/")

    def test_role_with_config_keys_apply(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "fast" {\n'
                '    agent "claude"\n'
                '    model "haiku"\n'
                '    effort "low"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "fast", "do x"],
                        **claude_responds("done"))
            invs = env.claude_invocations()
            self.assertEqual(invs[0]["parsed"]["model"], "haiku")

    def test_cli_overrides_role_config(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" { agent "claude"; model "opus" }\n'
            )
            env.run_cli(["new", "alice", "--role", "qa", "do x",
                         "--config", "model=haiku"],
                        **claude_responds("done"))
            invs = env.claude_invocations()
            self.assertEqual(invs[0]["parsed"]["model"], "haiku")

    def test_role_applies_via_mcp(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            with McpClient(env=env.env(**claude_responds("ok")),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor",
                    {"name": "alice", "role": "qa", "prompt": "review"},
                )
                client.recv_notification(timeout=15)
            invs = env.claude_invocations()
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"], "you are qa"
            )
            self.assertEqual(invs[0]["parsed"]["prompt"], "review")


if __name__ == "__main__":
    unittest.main()
