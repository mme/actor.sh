"""e2e: rare combinations users might hit."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RareCombinationTests(unittest.TestCase):

    def test_actor_main_cwd_doesnt_become_a_worktree(self):
        # `actor main` shouldn't create any actor row.
        with isolated_home() as env:
            import subprocess
            subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("ok")),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(env.list_actor_names(), [])

    def test_role_named_main_overrides_built_in_for_actor_main(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "main" {\n'
                '    agent "claude"\n'
                '    prompt "TEST_OVERRIDE_MAIN"\n'
                '}\n'
            )
            import subprocess
            subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("ok")),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            invs = env.claude_invocations()
            self.assertEqual(
                invs[0]["parsed"]["append_system_prompt"],
                "TEST_OVERRIDE_MAIN",
            )

    def test_setup_then_main_pipeline_works(self):
        with isolated_home() as env:
            env.run_cli(["setup", "--for", "claude-code"])
            import subprocess
            r = subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("ok")),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            # `actor main` should still work after setup.
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_default_class_permission_mode_applied(self):
        # Even with no settings.kdl, claude actors get permission-mode auto.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(invs[0]["parsed"]["permission_mode"], "auto")

    def test_default_class_use_subscription_true(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.actor_keys.get("use-subscription"), "true"
            )


if __name__ == "__main__":
    unittest.main()
