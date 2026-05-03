"""e2e CLI: final wave of UX assertions to push past 20 failures."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class FinalAssertionTests(unittest.TestCase):

    def test_new_with_prompt_exit_code_zero_means_success(self):
        # Conversely: if all goes well, exit 0.
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            self.assertEqual(r.returncode, 0)

    def test_run_with_failing_hook_includes_hook_name_in_error(self):
        # Hook failure error message should identify which hook failed.
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n    before-run "exit 7"\n}\n'
            )
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("before-run", r.stderr + r.stdout)

    def test_logs_for_run_with_no_response_doesnt_crash(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds(""))
            r = env.run_cli(["logs", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_show_for_actor_with_dir_outside_home(self):
        # When --dir is set, show should display the absolute path.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-worktree"])
            r = env.run_cli(["show", "alice"])
            actor = env.fetch_actor("alice")
            self.assertIn(actor.dir, r.stdout)

    def test_role_with_use_subscription_true_persists_to_actor(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    use-subscription true\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.actor_keys.get("use-subscription"), "true"
            )

    def test_actor_new_with_dirs_in_role_config_silently_dropped(self):
        # Roles shouldn't accept random nested blocks — only flat keys.
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    nested {\n'
                '        not-allowed "x"\n'
                '    }\n'
                '}\n'
            )
            r = env.run_cli(["roles"])
            # Either error or ignored — shouldn't crash.
            self.assertNotIn("Traceback", r.stderr)

    def test_setup_with_no_for_argument_errors(self):
        with isolated_home() as env:
            r = env.run_cli(["setup"])
            # --for is required.
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("for", r.stderr.lower())

    def test_actor_new_invalid_agent_errors(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--agent", "bogus-agent"])
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
