"""e2e MCP: config_actor."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


def _text(result: dict) -> str:
    return "\n".join(
        c.get("text", "") for c in result.get("content", [])
        if c.get("type") == "text"
    )


class McpConfigActorTests(unittest.TestCase):

    def test_view_config(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("config_actor", {"name": "alice"})
                t = _text(result)
                self.assertIn("model", t)
                self.assertIn("opus", t)

    def test_update_config(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "config_actor",
                    {"name": "alice", "pairs": ["model=sonnet"]},
                )
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "sonnet")

    def test_update_multiple_pairs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "config_actor",
                    {"name": "alice",
                     "pairs": ["model=opus", "effort=max"]},
                )
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "opus")
            self.assertEqual(actor.config.agent_args.get("effort"), "max")


if __name__ == "__main__":
    unittest.main()
