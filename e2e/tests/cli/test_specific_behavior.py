"""e2e: very specific behavioral assertions."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class SpecificBehaviorTests(unittest.TestCase):

    def test_actor_main_no_args_implies_interactive(self):
        # `actor main` with no args = interactive (no -p flag).
        import subprocess
        with isolated_home() as env:
            subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("ok")),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            invs = env.claude_invocations()
            self.assertFalse(invs[0]["parsed"]["print_mode"],
                             "actor main should not pass -p (interactive)")

    def test_actor_new_with_prompt_has_print_mode_p(self):
        # Sub-actors run non-interactively with -p.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertTrue(invs[0]["parsed"]["print_mode"])

    def test_role_description_appears_in_actor_roles_table(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    description "UNIQUE_DESCRIPTION_TEXT"\n'
                '    agent "claude"\n'
                '}\n'
            )
            r = env.run_cli(["roles"])
            self.assertIn("UNIQUE_DESCRIPTION_TEXT", r.stdout)

    def test_show_includes_use_subscription_value(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-use-subscription"])
            r = env.run_cli(["show", "alice"])
            # Show should display use-subscription somewhere.
            self.assertIn("use-subscription", r.stdout)

    def test_run_with_no_prompt_no_input_errors(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            # Empty stdin via input="" — should error friendly.
            r = env.run_cli(["run", "alice"], input="")
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
