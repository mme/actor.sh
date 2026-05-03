"""e2e: overriding the built-in `main` role works as documented."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class MainRoleOverrideTests(unittest.TestCase):

    def test_main_role_appears_in_actor_roles(self):
        with isolated_home() as env:
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("main", r.stdout)

    def test_main_role_overridable_in_user_settings(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "main" {\n'
                '    description "My custom orchestrator."\n'
                '    agent "claude"\n'
                '    prompt "you are my orchestrator"\n'
                '}\n'
            )
            r = env.run_cli(["roles"])
            self.assertIn("main", r.stdout)
            self.assertIn("My custom orchestrator", r.stdout)

    def test_main_role_overridable_in_project_settings(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "main" {\n'
                '    description "Project main role"\n'
                '    agent "claude"\n'
                '}\n',
                scope="project",
            )
            r = env.run_cli(["roles"])
            self.assertIn("Project main role", r.stdout)


if __name__ == "__main__":
    unittest.main()
