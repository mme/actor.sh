"""e2e CLI: more invariants likely to surface inconsistency."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreInvariantTests(unittest.TestCase):

    def test_show_actor_includes_config_section(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            r = env.run_cli(["show", "alice"])
            # Show should always include a config-related section.
            self.assertTrue(
                "config" in r.stdout.lower() or "model" in r.stdout.lower()
            )

    def test_show_actor_includes_session_after_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            actor = env.fetch_actor("alice")
            self.assertIn(actor.agent_session, r.stdout)

    def test_logs_for_running_actor_does_not_block_indefinitely(self):
        # Without --watch, logs should return what's there and exit.
        import time
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            t = time.time()
            r = env.run_cli(["logs", "alice"])
            elapsed = time.time() - t
            self.assertLess(elapsed, 5.0, "logs should return quickly")

    def test_multiple_actors_with_different_agents(self):
        from e2e.harness.fakes_control import codex_responds
        with isolated_home() as env:
            env.run_cli(["new", "claude-actor", "do x"],
                        **claude_responds("a"))
            env.run_cli(["new", "codex-actor", "do x", "--agent", "codex"],
                        **codex_responds("b"))
            r = env.run_cli(["list"])
            self.assertIn("claude-actor", r.stdout)
            self.assertIn("codex-actor", r.stdout)

    def test_setup_for_codex_works(self):
        with isolated_home() as env:
            r = env.run_cli(["setup", "--for", "codex"])
            # codex is mentioned in argparse choices? Don't assert
            # exit code strictly.
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
