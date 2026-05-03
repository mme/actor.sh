"""e2e MCP: list_actors tool."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


def _text(result: dict) -> str:
    """Extract the text portion of an MCP tool-call result."""
    content = result.get("content", [])
    return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")


class McpListActorsTests(unittest.TestCase):

    def test_list_empty_db(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("list_actors")
                self.assertIn("NAME", _text(result))

    def test_list_with_actors(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("list_actors")
                t = _text(result)
                self.assertIn("alice", t)
                self.assertIn("bob", t)

    def test_list_status_filter(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("list_actors",
                                          {"status": "running"})
                # alice is idle, not running
                self.assertNotIn("alice", _text(result))


if __name__ == "__main__":
    unittest.main()
