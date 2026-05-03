"""e2e: specific error / status messages users will rely on."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class SpecificMessageTests(unittest.TestCase):

    def test_unknown_actor_error_includes_name(self):
        with isolated_home() as env:
            r = env.run_cli(["show", "unique-ghost-xyz"])
            self.assertIn("unique-ghost-xyz", r.stderr + r.stdout)

    def test_duplicate_create_error_includes_name(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["new", "alice"])
            self.assertIn("alice", r.stderr + r.stdout)

    def test_unknown_role_error_includes_role_name(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "x", "--role", "unique-bad-role"])
            self.assertIn("unique-bad-role", r.stderr + r.stdout)

    def test_invalid_name_error_includes_name(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "Bad/Name"])
            self.assertIn("Bad/Name", r.stderr + r.stdout)

    def test_stderr_only_contains_errors_not_normal_output(self):
        # `actor list` on success should not print to stderr.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
