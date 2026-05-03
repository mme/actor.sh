"""e2e: more aggressive edge case probes."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreAggressiveTests(unittest.TestCase):

    def test_role_with_empty_prompt_string(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt ""\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa"])
            # Empty prompt should either be set to "" or treated as
            # absent; never crash.
            self.assertNotIn("Traceback", r.stderr)

    def test_role_with_only_whitespace_prompt(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "   "\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa"])
            self.assertNotIn("Traceback", r.stderr)

    def test_actor_name_underscore_then_hyphen(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "_-test"])
            self.assertNotIn("Traceback", r.stderr)

    def test_settings_kdl_inside_dot_actor_only(self):
        # Settings.kdl outside .actor/ should NOT be picked up.
        with isolated_home() as env:
            (env.cwd / "settings.kdl").write_text(
                'role "fake" {\n    agent "claude"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertNotIn("fake", r.stdout)

    def test_show_unknown_actor_says_not_found(self):
        with isolated_home() as env:
            r = env.run_cli(["show", "ghost"])
            self.assertNotEqual(r.returncode, 0)
            err = (r.stderr + r.stdout).lower()
            self.assertTrue(
                "not found" in err or "no actor" in err or "ghost" in err
            )

    def test_list_with_running_actor_shows_running_status(self):
        import subprocess, time
        with isolated_home() as env:
            p = subprocess.Popen(
                ["actor", "new", "alice", "long task"],
                env=env.env(**claude_responds("ok", sleep=2)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                r = env.run_cli(["list"])
                self.assertEqual(r.returncode, 0, msg=r.stderr)
                self.assertIn("running", r.stdout.lower())
            finally:
                p.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
