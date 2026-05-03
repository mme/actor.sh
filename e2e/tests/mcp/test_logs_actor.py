"""e2e MCP: logs_actor."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


def _text(result: dict) -> str:
    return "\n".join(
        c.get("text", "") for c in result.get("content", [])
        if c.get("type") == "text"
    )


class McpLogsActorTests(unittest.TestCase):

    def test_logs_for_completed_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("the answer is 42"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("logs_actor", {"name": "alice"})
                self.assertIn("42", _text(result))

    def test_logs_verbose_mode(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("answer", thinking="reason"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool(
                    "logs_actor", {"name": "alice", "verbose": True}
                )
                self.assertIn("reason", _text(result))


if __name__ == "__main__":
    unittest.main()
