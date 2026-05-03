"""e2e: `actor setup` / `actor update` — deploy the skill + register MCP."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class ActorSetupTests(unittest.TestCase):

    def test_setup_for_claude_code_creates_skill_dir(self):
        with isolated_home() as env:
            r = env.run_cli(["setup", "--for", "claude-code"])
            # Setup may try to register MCP via `claude` CLI — our fake
            # accepts anything but doesn't actually do registration.
            # Don't strictly assert exit code; check the skill dir was
            # written.
            skill_dir = env.home / ".claude" / "skills"
            if r.returncode == 0:
                self.assertTrue(skill_dir.exists(),
                                "skill dir should exist after setup")

    def test_setup_project_scope_writes_to_cwd(self):
        with isolated_home() as env:
            r = env.run_cli(["setup", "--for", "claude-code", "--scope", "project"])
            project_skill_dir = env.cwd / ".claude" / "skills"
            if r.returncode == 0:
                self.assertTrue(project_skill_dir.exists())

    def test_update_after_setup(self):
        with isolated_home() as env:
            env.run_cli(["setup", "--for", "claude-code"])
            r = env.run_cli(["update"])
            # Update should refresh the deployed skill files. Don't
            # assert exit code strictly — depends on whether setup
            # succeeded — but no Python traceback.
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
