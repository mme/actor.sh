"""e2e workflow: actor management combinations."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorManagementWorkflowTests(unittest.TestCase):

    def test_create_run_change_config_run_again(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("a"))
            env.run_cli(["config", "alice", "model=opus"])
            env.run_cli(["run", "alice", "do y"], **claude_responds("b"))
            invs = env.claude_invocations()
            # Last run should have used opus.
            self.assertEqual(invs[-1]["parsed"]["model"], "opus")

    def test_role_then_explicit_config_layered(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    model "opus"\n'
                '    effort "max"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x", "--role", "qa",
                         "--config", "model=haiku"], **claude_responds("ok"))
            invs = env.claude_invocations()
            # CLI config wins for model; effort from role survives.
            self.assertEqual(invs[0]["parsed"]["model"], "haiku")
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("effort"), "max")

    def test_two_actors_different_roles_dont_cross_contaminate(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "fast" {\n    agent "claude"\n    model "haiku"\n}\n'
                'role "slow" {\n    agent "claude"\n    model "opus"\n}\n'
            )
            env.run_cli(["new", "fast-actor", "--role", "fast"])
            env.run_cli(["new", "slow-actor", "--role", "slow"])
            fast = env.fetch_actor("fast-actor")
            slow = env.fetch_actor("slow-actor")
            self.assertEqual(fast.config.agent_args.get("model"), "haiku")
            self.assertEqual(slow.config.agent_args.get("model"), "opus")

    def test_discard_then_recreate_same_name_with_no_worktree_works(self):
        # Without a worktree, no branch is created; recreate is fine.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-worktree"])
            env.run_cli(["discard", "alice", "--force"])
            r = env.run_cli(["new", "alice", "--no-worktree"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_discard_then_recreate_same_name_after_branch_cleanup(self):
        # With a worktree, discard intentionally leaves the git branch
        # so committed work isn't lost (default on-discard hook only
        # checks unstaged modifications). Recovery for the recreate
        # case is to remove the branch manually.
        import subprocess
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            source_repo = actor.source_repo
            env.run_cli(["discard", "alice", "--force"])
            # Manual cleanup the user would do after seeing
            # "branch already exists":
            subprocess.run(
                ["git", "branch", "-D", "alice"],
                cwd=source_repo, check=True, capture_output=True,
            )
            r = env.run_cli(["new", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
