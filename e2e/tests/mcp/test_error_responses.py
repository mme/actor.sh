"""e2e MCP: tools return clear error responses on bad input."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class McpErrorResponseTests(unittest.TestCase):

    def test_show_actor_unknown_name(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("show_actor", {"name": "ghost"})
                # Either isError=True or content mentions not-found.
                content_text = "\n".join(
                    c.get("text", "") for c in result.get("content", [])
                )
                self.assertTrue(
                    result.get("isError") or "not found" in content_text.lower()
                    or "ghost" in content_text.lower(),
                    f"expected friendly not-found, got: {result}",
                )

    def test_stop_actor_unknown_name(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("stop_actor", {"name": "ghost"})
                self.assertNotIn("Traceback", str(result))

    def test_discard_actor_unknown_name(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("discard_actor", {"name": "ghost"})
                self.assertNotIn("Traceback", str(result))

    def test_new_actor_invalid_name(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool(
                    "new_actor", {"name": "bad/name"}
                )
                self.assertTrue(
                    result.get("isError") or "invalid" in str(result).lower()
                )

    def test_new_actor_duplicate_name(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("new_actor", {"name": "alice"})
                # Duplicate must be flagged, not silently overwriting.
                self.assertTrue(
                    result.get("isError") or "exists" in str(result).lower()
                )

    def test_run_actor_unknown_name(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool(
                    "run_actor", {"name": "ghost", "prompt": "x"}
                )
                self.assertNotIn("Traceback", str(result))


if __name__ == "__main__":
    unittest.main()
