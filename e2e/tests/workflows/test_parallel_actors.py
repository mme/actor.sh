"""e2e workflow: spawn N actors in parallel; all complete; correct accounting."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class ParallelActorsTests(unittest.TestCase):

    def test_five_parallel_actors_all_complete(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("ok", sleep=0.3)),
                           cwd=env.cwd) as client:
                client.initialize()
                names = [f"actor-{i}" for i in range(5)]
                for n in names:
                    client.call_tool(
                        "new_actor", {"name": n, "prompt": f"task for {n}"}
                    )
                received = []
                for _ in range(5):
                    note = client.recv_notification(timeout=30)
                    received.append(note["params"]["meta"]["actor"])
            self.assertEqual(set(received), set(names))

    def test_parallel_completion_notifications_carry_status(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("ok", sleep=0.2)),
                           cwd=env.cwd) as client:
                client.initialize()
                for n in ("a1", "a2", "a3"):
                    client.call_tool(
                        "new_actor", {"name": n, "prompt": "x"}
                    )
                statuses = []
                for _ in range(3):
                    note = client.recv_notification(timeout=20)
                    statuses.append(note["params"]["meta"]["status"])
            for s in statuses:
                self.assertEqual(s, "done")


if __name__ == "__main__":
    unittest.main()
