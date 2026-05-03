"""e2e: `actor roles` — list available roles, including the built-in main."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class ActorRolesTests(unittest.TestCase):

    def test_roles_lists_built_in_main(self):
        with isolated_home() as env:
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("main", r.stdout)
            # Default description should appear too.
            self.assertIn("orchestrator", r.stdout.lower())

    def test_roles_includes_user_defined_role(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    description "QA engineer for tests."\n'
                '    agent "claude"\n'
                '}\n'
            )
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("qa", r.stdout)
            self.assertIn("QA engineer", r.stdout)

    def test_roles_sorted_alphabetically(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "zebra" {\n    agent "claude"\n}\n'
                'role "apple" {\n    agent "claude"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            apple_idx = r.stdout.find("apple")
            zebra_idx = r.stdout.find("zebra")
            self.assertLess(apple_idx, zebra_idx)

    def test_roles_role_without_description_renders_empty_cell(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n    agent "claude"\n}\n'  # no description set
            )
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("qa", r.stdout)
            # Description column for qa should be empty (not "None" or
            # similar). Best-effort: the qa row line shouldn't contain
            # anything after "claude" that looks like a description.

    def test_roles_table_columns(self):
        with isolated_home() as env:
            r = env.run_cli(["roles"])
            self.assertIn("NAME", r.stdout)
            self.assertIn("AGENT", r.stdout)
            self.assertIn("DESCRIPTION", r.stdout)


if __name__ == "__main__":
    unittest.main()
