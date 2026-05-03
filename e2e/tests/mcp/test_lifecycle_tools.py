"""e2e MCP: show_actor, logs_actor, config_actor, stop_actor, discard_actor."""
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


class McpLogsActorTests(unittest.TestCase):

    def test_logs_for_completed_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("the answer is 42"))
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("logs_actor", {"name": "alice"})
                self.assertIn("42", _text(result))


class McpConfigActorTests(unittest.TestCase):

    def test_view_config(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("config_actor", {"name": "alice"})
                self.assertIn("model", _text(result))
                self.assertIn("opus", _text(result))

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


class McpDiscardActorTests(unittest.TestCase):

    def test_discard_clean_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool("discard_actor", {"name": "alice"})
            self.assertEqual(env.list_actor_names(), [])

    def test_discard_with_force(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            from pathlib import Path
            (Path(actor.dir) / "dirty.txt").write_text("x")
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "discard_actor", {"name": "alice", "force": True}
                )
            self.assertEqual(env.list_actor_names(), [])


if __name__ == "__main__":
    unittest.main()
