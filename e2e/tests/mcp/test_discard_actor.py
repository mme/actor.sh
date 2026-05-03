"""e2e MCP: discard_actor."""
from __future__ import annotations

import unittest
from pathlib import Path

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class McpDiscardActorTests(unittest.TestCase):

    def test_discard_clean_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool("discard_actor", {"name": "alice"})
            self.assertEqual(env.list_actor_names(), [])

    def test_discard_dirty_blocked_without_force(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            # Modify a tracked file (README.md from the harness's git
            # init) so the default `git diff --quiet` flags it.
            (Path(actor.dir) / "README.md").write_text("modified")
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool("discard_actor", {"name": "alice"})
            self.assertIn("alice", env.list_actor_names())

    def test_discard_with_force(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            (Path(actor.dir) / "README.md").write_text("modified")
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "discard_actor", {"name": "alice", "force": True}
                )
            self.assertEqual(env.list_actor_names(), [])

    def test_discard_running_actor_stops_first(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("ok", sleep=5)),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor", {"name": "alice", "prompt": "x"}
                )
                import time
                time.sleep(0.5)
                client.call_tool(
                    "discard_actor",
                    {"name": "alice", "force": True},
                )
            self.assertEqual(env.list_actor_names(), [])


if __name__ == "__main__":
    unittest.main()
