"""e2e MCP: run_actor — fires background run, returns immediately."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class McpRunActorTests(unittest.TestCase):

    def test_run_returns_immediately_then_notifies(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(**claude_responds("ok", sleep=0.5)),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "run_actor", {"name": "alice", "prompt": "do x"}
                )
                # Notification should arrive after fake sleeps + completes.
                note = client.recv_notification(timeout=15)
                self.assertEqual(
                    note["params"]["meta"]["actor"], "alice"
                )
                self.assertEqual(
                    note["params"]["meta"]["status"], "done"
                )

    def test_run_per_call_config_override(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            with McpClient(env=env.env(**claude_responds("ok")),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "run_actor",
                    {"name": "alice", "prompt": "do x",
                     "config": ["model=haiku"]},
                )
                client.recv_notification(timeout=15)
            invs = env.claude_invocations()
            self.assertEqual(invs[-1]["parsed"]["model"], "haiku")


if __name__ == "__main__":
    unittest.main()
