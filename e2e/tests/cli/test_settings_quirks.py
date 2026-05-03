"""e2e: settings.kdl quirks and corner cases."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class SettingsQuirkTests(unittest.TestCase):

    def test_empty_settings_kdl_is_valid(self):
        with isolated_home() as env:
            env.write_settings_kdl("")
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_settings_with_only_comments_valid(self):
        with isolated_home() as env:
            env.write_settings_kdl("// just a comment\n")
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_settings_with_role_then_comment(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    // comment in role\n'
                '}\n'
                '// comment at top level\n'
            )
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("qa", r.stdout)

    def test_role_with_blank_lines_in_body(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '\n'
                '    agent "claude"\n'
                '\n'
                '    model "opus"\n'
                '\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
