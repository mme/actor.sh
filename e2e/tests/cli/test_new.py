"""e2e: `actor new` — every shape from the plan's coverage matrix."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class ActorNewTests(unittest.TestCase):

    def test_new_idle_creates_actor_no_run(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("alice", r.stdout)
            self.assertEqual(env.list_actor_names(), ["alice"])
            self.assertEqual(env.claude_invocations(), [])

    def test_new_with_prompt_creates_and_runs(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "do x"], **claude_responds("OK"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 1)
            self.assertEqual(invs[0]["parsed"]["prompt"], "do x")

    def test_new_prompt_via_stdin(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice"], input="do via stdin",
                            **claude_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 1)
            self.assertEqual(invs[0]["parsed"]["prompt"], "do via stdin")

    def test_new_codex_agent(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "do x", "--agent", "codex"],
                            **codex_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(len(env.codex_invocations()), 1)
            self.assertEqual(env.claude_invocations(), [])

    def test_new_no_worktree(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--no-worktree"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertFalse(actor.worktree)

    def test_new_with_dir_targets_other_repo(self):
        with isolated_home() as env:
            # Stand up a sibling repo to spawn into.
            import os, subprocess
            other = env.cwd.parent / "other-repo"
            other.mkdir()
            git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e@x",
                "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e@x",
            }
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other,
                           check=True, env=git_env, capture_output=True)
            (other / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=other, check=True,
                           env=git_env, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=other,
                           check=True, env=git_env, capture_output=True)
            r = env.run_cli(["new", "alice", "--dir", str(other)])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertIn(str(other), actor.source_repo or "")

    def test_new_use_subscription_true_persists(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--use-subscription"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.actor_keys.get("use-subscription"), "true"
            )

    def test_new_with_base_branch(self):
        with isolated_home() as env:
            # create a branch to fork from
            import subprocess
            subprocess.run(["git", "checkout", "-b", "develop"], cwd=env.cwd,
                           check=True, capture_output=True)
            r = env.run_cli(["new", "alice", "--base", "develop"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_new_with_config_pair(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--config", "model=opus"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "opus")

    def test_new_use_subscription_flag(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--no-use-subscription"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.actor_keys.get("use-subscription"),
                             "false")

    def test_new_with_role_no_prompt_creates_idle(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(env.claude_invocations(), [])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.agent_args.get("append-system-prompt"),
                "you are qa",
            )

    def test_new_with_role_and_prompt_runs_with_task(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "review src/", "--role", "qa"],
                            **claude_responds("done"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 1)
            self.assertEqual(invs[0]["parsed"]["prompt"], "review src/")
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"], "you are qa"
            )

    def test_new_with_unknown_role_lists_available(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "missing"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("qa", r.stderr + r.stdout)

    def test_new_with_codex_role_having_prompt_errors(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "codex"\n'
                '    prompt "system"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa", "do x"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("codex", r.stderr.lower())

    def test_new_duplicate_name_errors(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["new", "alice"])
            self.assertNotEqual(r.returncode, 0)

    def test_new_invalid_name_errors(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "Invalid Name With Spaces"])
            self.assertNotEqual(r.returncode, 0)

    def test_new_runs_on_start_hook(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-start "echo HOOK_RAN > $ACTOR_DIR/hook.txt"\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            from pathlib import Path
            hook_marker = Path(actor.dir) / "hook.txt"
            self.assertTrue(hook_marker.exists(),
                            f"on-start hook should have created marker at {hook_marker}")

    def test_new_on_start_hook_failure_rolls_back(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n    on-start "exit 1"\n}\n'
            )
            r = env.run_cli(["new", "alice"])
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(env.list_actor_names(), [])


if __name__ == "__main__":
    unittest.main()
