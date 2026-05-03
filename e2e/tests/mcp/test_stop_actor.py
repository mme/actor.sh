"""e2e MCP: stop_actor."""
from __future__ import annotations

import time
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class McpStopActorTests(unittest.TestCase):

    def test_stop_running_actor(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("ok", sleep=5)),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor", {"name": "alice", "prompt": "x"}
                )
                # Give the spawned run a moment to land in RUNNING state.
                time.sleep(0.5)
                client.call_tool("stop_actor", {"name": "alice"})
                # Wait for completion notification.
                client.recv_notification(timeout=10)
            with env.db() as db:
                run = db.latest_run("alice")
                self.assertIn(run.status.as_str(), ("stopped", "error"))

    def test_stop_idle_actor_does_not_crash(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("stop_actor", {"name": "alice"})
                # Either succeeds quietly or returns a friendly error;
                # never a Python traceback.
                self.assertNotIn("Traceback", str(result))


if __name__ == "__main__":
    unittest.main()
