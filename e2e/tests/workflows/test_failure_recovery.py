"""e2e workflow: failure modes and recovery."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class FailureRecoveryTests(unittest.TestCase):

    def test_claude_nonzero_exit_marks_run_error(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("oops", exit=2))
            with env.db() as db:
                run = db.latest_run("alice")
                self.assertEqual(run.status.as_str(), "error")
                self.assertEqual(run.exit_code, 2)

    def test_claude_sigkill_eventually_marked_error(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds(crash="SIGKILL"))
            with env.db() as db:
                run = db.latest_run("alice")
                self.assertIn(run.status.as_str(), ("error", "stopped"))

    def test_on_start_hook_failure_rolls_back_actor_and_worktree(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks { on-start "exit 7" }\n'
            )
            r = env.run_cli(["new", "alice"])
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(env.list_actor_names(), [])
            # Worktree should not exist either.
            wt = env.home / ".actor" / "worktrees" / "alice"
            self.assertFalse(wt.exists())


if __name__ == "__main__":
    unittest.main()
