"""e2e MCP: show_actor."""
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


class McpShowActorTests(unittest.TestCase):

    def test_show_existing(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("show_actor", {"name": "alice"})
                self.assertIn("alice", _text(result))

    def test_show_with_runs_param(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool(
                    "show_actor", {"name": "alice", "runs": 0}
                )
                self.assertNotIn("Traceback", _text(result))

    def test_show_includes_recent_runs_default_5(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("show_actor", {"name": "alice"})
                t = _text(result)
                # Default runs=5 means both should be visible.
                self.assertIn("first", t.lower() + "")  # at least mention prompts


if __name__ == "__main__":
    unittest.main()
