"""e2e MCP: completion notifications via channel."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class ChannelNotificationTests(unittest.TestCase):

    def test_completion_notification_carries_actor_name(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("ok")),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor", {"name": "alice", "prompt": "x"}
                )
                note = client.recv_notification(timeout=15)
                self.assertEqual(note["params"]["meta"]["actor"], "alice")

    def test_error_status_when_fake_exits_nonzero(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("oops", exit=2)),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor", {"name": "alice", "prompt": "x"}
                )
                note = client.recv_notification(timeout=15)
                self.assertEqual(note["params"]["meta"]["status"], "error")

    def test_parallel_actors_correct_actor_names(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("ok", sleep=0.3)),
                           cwd=env.cwd) as client:
                client.initialize()
                for name in ("alice", "bob", "carol"):
                    client.call_tool(
                        "new_actor", {"name": name, "prompt": f"task {name}"}
                    )
                received = []
                for _ in range(3):
                    note = client.recv_notification(timeout=20)
                    received.append(note["params"]["meta"]["actor"])
            self.assertEqual(set(received), {"alice", "bob", "carol"},
                             f"got: {received}")


if __name__ == "__main__":
    unittest.main()
