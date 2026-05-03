"""e2e MCP: new_actor — create + optionally run."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class McpNewActorTests(unittest.TestCase):

    def test_create_idle(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool("new_actor", {"name": "alice"})
            self.assertEqual(env.list_actor_names(), ["alice"])

    def test_create_with_prompt_runs_in_background(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("ok")),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool("new_actor",
                                 {"name": "alice", "prompt": "do x"})
                # Wait for the channel notification.
                note = client.recv_notification(timeout=15)
                self.assertEqual(note["method"], "notifications/claude/channel")
                self.assertEqual(
                    note["params"]["meta"]["actor"], "alice"
                )

    def test_create_with_role(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor", {"name": "alice", "role": "qa"}
                )
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.agent_args.get("append-system-prompt"),
                "you are qa",
            )

    def test_create_with_dir_uses_that_path(self):
        with isolated_home() as env:
            # Create a sibling repo to spawn into.
            import subprocess
            from pathlib import Path
            other = env.cwd.parent / "other-repo"
            other.mkdir()
            for c in ("git init -q -b main", "git config user.email e2e@x",
                      "git config user.name e2e"):
                subprocess.run(c.split(), cwd=other, check=True,
                               capture_output=True)
            (other / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=other, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=other,
                           check=True, capture_output=True,
                           env={**__import__("os").environ,
                                "GIT_AUTHOR_NAME": "e2e",
                                "GIT_AUTHOR_EMAIL": "e@x",
                                "GIT_COMMITTER_NAME": "e2e",
                                "GIT_COMMITTER_EMAIL": "e@x"})
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor", {"name": "alice", "dir": str(other)}
                )
            actor = env.fetch_actor("alice")
            self.assertIn(str(other), actor.source_repo or "")


if __name__ == "__main__":
    unittest.main()
