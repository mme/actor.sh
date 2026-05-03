"""e2e MCP: full workflow with assertions on each step."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class McpWorkflowTests(unittest.TestCase):

    def test_create_then_run_then_show_then_discard(self):
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("done")),
                           cwd=env.cwd) as client:
                client.initialize()
                # 1. create idle
                client.call_tool("new_actor", {"name": "alice"})
                # 2. list shows idle
                self.assertIn("alice", str(client.call_tool("list_actors")))
                # 3. run with prompt → notification
                client.call_tool(
                    "run_actor", {"name": "alice", "prompt": "do x"}
                )
                note = client.recv_notification(timeout=15)
                self.assertEqual(note["params"]["meta"]["actor"], "alice")
                # 4. show actor with run details
                show = client.call_tool("show_actor", {"name": "alice"})
                self.assertIn("alice", str(show))
                # 5. discard
                client.call_tool(
                    "discard_actor", {"name": "alice", "force": True}
                )
            self.assertEqual(env.list_actor_names(), [])

    def test_create_with_role_then_run(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '    model "opus"\n'
                '}\n'
            )
            with McpClient(env=env.env(**claude_responds("done")),
                           cwd=env.cwd) as client:
                client.initialize()
                client.call_tool(
                    "new_actor", {"name": "alice", "role": "qa"}
                )
                client.call_tool(
                    "run_actor",
                    {"name": "alice", "prompt": "review src/"},
                )
                client.recv_notification(timeout=15)
            invs = env.claude_invocations()
            self.assertEqual(invs[0]["parsed"]["model"], "opus")
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"], "you are qa"
            )


if __name__ == "__main__":
    unittest.main()
