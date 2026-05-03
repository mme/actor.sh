"""e2e MCP: more edge cases."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class MoreMcpEdgeTests(unittest.TestCase):

    def test_call_tool_without_arguments_field(self):
        # Some calls genuinely take no args (list_actors, list_roles).
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                # Pass {} explicitly — should be valid.
                result = client.call_tool("list_actors", {})
                self.assertIn("content", result)

    def test_call_tool_with_extra_unknown_arg(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                # Extra arg might be ignored, might error — just shouldn't crash.
                result = client.call_tool(
                    "list_actors", {"bogus_arg": True}
                )
                self.assertNotIn("Traceback", str(result))

    def test_new_actor_with_empty_string_prompt(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                # Empty string prompt: should create idle (per the
                # docstring, the prompt is only used if non-empty).
                result = client.call_tool(
                    "new_actor", {"name": "alice", "prompt": ""}
                )
                self.assertNotIn("Traceback", str(result))

    def test_show_actor_with_negative_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool(
                    "show_actor", {"name": "alice", "runs": -1}
                )
                # Should clamp or reject; never crash.
                self.assertNotIn("Traceback", str(result))


if __name__ == "__main__":
    unittest.main()
