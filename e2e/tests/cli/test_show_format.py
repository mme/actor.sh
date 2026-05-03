"""e2e: actor show output format checks."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ShowFormatTests(unittest.TestCase):

    def test_show_includes_session_id_after_first_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            actor = env.fetch_actor("alice")
            r = env.run_cli(["show", "alice"])
            # Show should display the session id somewhere.
            self.assertIn(actor.agent_session, r.stdout)

    def test_show_includes_branch_for_worktree_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            # Worktree-based actor → branch == name. Display it.
            self.assertIn("alice", r.stdout)

    def test_show_does_not_leak_password_like_chars(self):
        # Sanity: nothing in show should look like an env-leak.
        with isolated_home() as env:
            env.run_cli(["new", "alice"], ANTHROPIC_API_KEY="sk-secret-token")
            r = env.run_cli(["show", "alice"])
            self.assertNotIn("sk-secret-token", r.stdout)


if __name__ == "__main__":
    unittest.main()
