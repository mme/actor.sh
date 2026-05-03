"""e2e MCP: repeated tool calls behave correctly."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class RepeatCallTests(unittest.TestCase):

    def test_list_actors_called_repeatedly(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                for _ in range(5):
                    result = client.call_tool("list_actors")
                    self.assertIn("alice", str(result))

    def test_show_actor_called_repeatedly(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                for _ in range(3):
                    result = client.call_tool(
                        "show_actor", {"name": "alice"}
                    )
                    self.assertIn("alice", str(result))


if __name__ == "__main__":
    unittest.main()
