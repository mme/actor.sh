"""e2e workflow: actor main → orchestrator → MCP-spawned sub-actor.

This is the headline workflow: the user runs `actor main`, the
orchestrator (running fake claude) calls `mcp__actor__new_actor` to
spawn a sub-actor, the sub-actor runs (also fake claude), and the
completion notification reaches the orchestrator. We exercise this
in pieces (we can't fully simulate the orchestrator's MCP calls
without a real LLM), so the test asserts the prerequisites: `actor
main` launches with the right system prompt + channel, and
sub-actors spawned via MCP behave correctly while the orchestrator
session is running."""
from __future__ import annotations

import subprocess
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


class OrchestratorSessionTests(unittest.TestCase):

    def test_actor_main_loads_orchestrator_system_prompt(self):
        with isolated_home() as env:
            r = subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("ok", exit=0)),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 1)
            sp = invs[0]["parsed"]["append_system_prompt"]
            self.assertIsNotNone(sp)
            # Spot-check key phrases from the orchestrator prompt.
            self.assertIn("Master Orchestrator", sp)
            self.assertIn("actor", sp.lower())

    def test_mcp_can_spawn_sub_actor_during_session(self):
        # Simulate "orchestrator session is up; orchestrator's MCP
        # client spawns a sub-actor mid-session". We use our test MCP
        # client as the orchestrator surrogate.
        with isolated_home() as env:
            with McpClient(env=env.env(**claude_responds("done")),
                           cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool(
                    "new_actor",
                    {"name": "subactor", "prompt": "delegated task"},
                )
                self.assertNotIn("error", str(result).lower())
                note = client.recv_notification(timeout=15)
                self.assertEqual(
                    note["params"]["meta"]["actor"], "subactor"
                )
                self.assertEqual(
                    note["params"]["meta"]["status"], "done"
                )


if __name__ == "__main__":
    unittest.main()
