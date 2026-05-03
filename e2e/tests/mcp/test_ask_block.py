"""e2e MCP: ask block strings appear in tool descriptions on the wire."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class AskBlockOnTheWireTests(unittest.TestCase):

    def test_default_appendix_present(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                tools = {t["name"]: t for t in client.list_tools()}
                self.assertIn("AskUserQuestion", tools["new_actor"]["description"])

    def test_user_string_overrides_default(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'ask {\n    on-start "CUSTOM_USER_GUIDANCE"\n}\n'
            )
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                tools = {t["name"]: t for t in client.list_tools()}
                self.assertIn("CUSTOM_USER_GUIDANCE",
                              tools["new_actor"]["description"])
                self.assertNotIn("AskUserQuestion",
                                 tools["new_actor"]["description"])

    def test_null_silences_default(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'ask {\n    on-discard null\n}\n'
            )
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                tools = {t["name"]: t for t in client.list_tools()}
                self.assertNotIn("AskUserQuestion",
                                 tools["discard_actor"]["description"])

    def test_project_overrides_user_per_key(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'ask {\n'
                '    on-start "USER_START"\n'
                '    before-run "USER_RUN"\n'
                '}\n',
                scope="user",
            )
            env.write_settings_kdl(
                'ask {\n    on-start "PROJECT_START"\n}\n',
                scope="project",
            )
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                tools = {t["name"]: t for t in client.list_tools()}
                self.assertIn("PROJECT_START",
                              tools["new_actor"]["description"])
                # user's before-run not overridden, still present
                self.assertIn("USER_RUN",
                              tools["run_actor"]["description"])


if __name__ == "__main__":
    unittest.main()
