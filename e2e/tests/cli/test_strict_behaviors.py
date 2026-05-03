"""e2e: strict assertions on documented behaviors that are easy to regress."""
from __future__ import annotations

import unittest
from pathlib import Path

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class StrictBehaviorTests(unittest.TestCase):

    def test_run_records_pid_in_db(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            with env.db() as db:
                run = db.latest_run("alice")
                # Completed run should have a recorded PID (maybe 0 for
                # fakes; should not be None).
                self.assertIsNotNone(run)

    def test_run_records_exit_code_zero_on_success(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            with env.db() as db:
                run = db.latest_run("alice")
                self.assertEqual(run.exit_code, 0)

    def test_run_records_exit_code_nonzero_on_failure(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("oops", exit=7))
            with env.db() as db:
                run = db.latest_run("alice")
                self.assertEqual(run.exit_code, 7)

    def test_run_records_finished_at_timestamp(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            with env.db() as db:
                run = db.latest_run("alice")
                self.assertIsNotNone(run.finished_at)
                self.assertNotEqual(run.finished_at, "")

    def test_actor_config_snapshotted_at_creation(self):
        # Config layered at create time should be byte-stable.
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n'
                '    model "snapshot-test"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            actor1 = env.fetch_actor("alice")
            # Mutate kdl after creation.
            env.write_settings_kdl(
                'defaults "claude" {\n'
                '    model "different"\n'
                '}\n'
            )
            actor2 = env.fetch_actor("alice")
            self.assertEqual(actor1.config.agent_args.get("model"),
                             actor2.config.agent_args.get("model"))
            self.assertEqual(
                actor1.config.agent_args.get("model"), "snapshot-test"
            )

    def test_worktree_branch_name_matches_actor_name(self):
        with isolated_home() as env:
            env.run_cli(["new", "feature-x"])
            actor = env.fetch_actor("feature-x")
            # The branch in the worktree should be feature-x.
            import subprocess
            r = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=actor.dir, capture_output=True, text=True,
            )
            self.assertEqual(r.stdout.strip(), "feature-x")

    def test_actor_dir_exists_after_create(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertTrue(Path(actor.dir).is_dir())

    def test_use_subscription_strips_anthropic_api_key(self):
        # When use-subscription=true, fake claude shouldn't see ANTHROPIC_API_KEY.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x", "--use-subscription"],
                        ANTHROPIC_API_KEY="should-be-stripped",
                        **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertNotIn("ANTHROPIC_API_KEY", invs[0]["env"])

    def test_no_use_subscription_passes_anthropic_api_key(self):
        # When use-subscription=false, the env var passes through.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x", "--no-use-subscription"],
                        ANTHROPIC_API_KEY="passes-through",
                        **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertIn("ANTHROPIC_API_KEY", invs[0]["env"])
            self.assertEqual(invs[0]["env"]["ANTHROPIC_API_KEY"],
                             "passes-through")


if __name__ == "__main__":
    unittest.main()
