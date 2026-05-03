"""e2e CLI: more assertions on subtle behaviors."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ExtraAssertionTests(unittest.TestCase):

    def test_role_prompt_field_appears_in_show(self):
        # If a role's prompt was applied, the actor's show output
        # should reference it (as the system prompt being active).
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            r = env.run_cli(["show", "alice"])
            self.assertIn("you are qa", r.stdout)

    def test_after_run_hook_fires_for_failed_run_too(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_EXIT_CODE > $HOME/exitcode.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("oops", exit=2))
            self.assertTrue((env.home / "exitcode.txt").is_file())
            self.assertEqual(
                (env.home / "exitcode.txt").read_text().strip(), "2"
            )

    def test_actor_main_with_role_codex_says_unsupported(self):
        # actor main only supports claude. With codex, the error message
        # should be specific.
        import subprocess
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "main" {\n'
                '    agent "codex"\n'
                '    prompt "x"\n'
                '}\n'
            )
            r = subprocess.run(
                ["actor", "main"],
                env=env.env(),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("codex", r.stderr.lower())
            self.assertIn("claude", r.stderr.lower())


if __name__ == "__main__":
    unittest.main()
