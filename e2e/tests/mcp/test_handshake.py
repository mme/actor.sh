"""e2e: MCP server handshake + tool list."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


EXPECTED_TOOLS = {
    "list_actors", "show_actor", "logs_actor", "stop_actor",
    "discard_actor", "config_actor", "new_actor", "run_actor",
    "list_roles",
}


class McpHandshakeTests(unittest.TestCase):

    def test_initialize_returns_server_info(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                init = client.initialize()
                self.assertIn("serverInfo", init)
                self.assertEqual(init["serverInfo"]["name"], "actor.sh")

    def test_tools_list_returns_expected_set(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                tools = client.list_tools()
                names = {t["name"] for t in tools}
                missing = EXPECTED_TOOLS - names
                self.assertEqual(missing, set(),
                                 f"missing expected MCP tools: {missing}")

    def test_tool_descriptions_have_default_ask_appendix(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                tools = {t["name"]: t for t in client.list_tools()}
                # Default ask.on-start references AskUserQuestion — should
                # land in new_actor's description.
                self.assertIn("AskUserQuestion", tools["new_actor"]["description"])
                self.assertIn("AskUserQuestion", tools["run_actor"]["description"])
                self.assertIn("AskUserQuestion", tools["discard_actor"]["description"])

    def test_tool_descriptions_unaffected_by_ask_for_other_tools(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                tools = {t["name"]: t for t in client.list_tools()}
                self.assertNotIn("AskUserQuestion", tools["list_actors"]["description"])
                self.assertNotIn("AskUserQuestion", tools["show_actor"]["description"])
                self.assertNotIn("AskUserQuestion", tools["list_roles"]["description"])


if __name__ == "__main__":
    unittest.main()
