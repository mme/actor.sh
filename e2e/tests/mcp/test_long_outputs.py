"""e2e MCP: tools handle large output."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class LongOutputTests(unittest.TestCase):

    def test_logs_with_long_response(self):
        # Fake claude response of ~50KB.
        big_response = "line " * 10_000
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds(big_response))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("logs_actor", {"name": "alice"})
                # Should not crash; should contain at least part of the output.
                self.assertNotIn("Traceback", str(result))

    def test_show_actor_with_many_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            for i in range(15):
                env.run_cli(["run", "alice", f"task {i}"],
                            **claude_responds("ok"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool(
                    "show_actor", {"name": "alice", "runs": 50}
                )
                self.assertNotIn("Traceback", str(result))


if __name__ == "__main__":
    unittest.main()
