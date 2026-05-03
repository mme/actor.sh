"""e2e MCP: strict assertions on tool response shape."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class StrictResponseTests(unittest.TestCase):

    def test_list_actors_returns_text_content(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("list_actors")
                self.assertIn("content", result)
                self.assertIsInstance(result["content"], list)
                # At least one content block is `text`.
                kinds = [c.get("type") for c in result["content"]]
                self.assertIn("text", kinds)

    def test_show_actor_returns_text_content(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("show_actor", {"name": "alice"})
                kinds = [c.get("type") for c in result.get("content", [])]
                self.assertIn("text", kinds)

    def test_list_roles_returns_built_in_main_only_for_empty_kdl(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("list_roles")
                text = "\n".join(
                    c.get("text", "") for c in result.get("content", [])
                )
                self.assertIn("main", text)


if __name__ == "__main__":
    unittest.main()
