"""e2e CLI: very specific UX expectations."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreSpecificUxTests(unittest.TestCase):

    def test_show_indicates_session_active_after_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            # Should mention "session" or similar.
            self.assertIn("session", r.stdout.lower())

    def test_show_includes_run_count(self):
        with isolated_home() as env:
            for i in range(3):
                env.run_cli(["new" if i == 0 else "run", "alice",
                             f"task-{i}"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            # Should reference 3 runs somehow.
            self.assertIn("3", r.stdout)

    def test_actor_dir_displayed_in_show_resolves(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            r = env.run_cli(["show", "alice"])
            # The exact dir path should appear.
            self.assertIn(actor.dir, r.stdout)

    def test_config_view_groups_actor_keys_separately(self):
        # actor_keys (use-subscription) vs agent_args (model, etc.)
        # — the view should distinguish them somehow.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus",
                         "--no-use-subscription"])
            r = env.run_cli(["config", "alice"])
            # Both should be visible.
            self.assertIn("model", r.stdout)
            self.assertIn("use-subscription", r.stdout)

    def test_logs_includes_user_prompts_in_order(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "FIRST_PROMPT"], **claude_responds("a"))
            env.run_cli(["run", "alice", "SECOND_PROMPT"], **claude_responds("b"))
            r = env.run_cli(["logs", "alice"])
            first_pos = r.stdout.find("FIRST_PROMPT")
            second_pos = r.stdout.find("SECOND_PROMPT")
            self.assertNotEqual(first_pos, -1, "FIRST_PROMPT should appear")
            self.assertNotEqual(second_pos, -1, "SECOND_PROMPT should appear")
            self.assertLess(first_pos, second_pos, "prompts should appear in order")


if __name__ == "__main__":
    unittest.main()
