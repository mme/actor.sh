"""e2e: `actor --help` and per-subcommand help."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class HelpOutputTests(unittest.TestCase):

    def test_top_level_help(self):
        with isolated_home() as env:
            r = env.run_cli(["--help"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("new", r.stdout)
            self.assertIn("run", r.stdout)
            self.assertIn("list", r.stdout)
            self.assertIn("main", r.stdout)
            self.assertIn("roles", r.stdout)

    def test_new_help_mentions_role(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "--help"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("--role", r.stdout)

    def test_new_help_mentions_config(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "--help"])
            self.assertIn("--config", r.stdout)

    def test_run_help_mentions_interactive(self):
        with isolated_home() as env:
            r = env.run_cli(["run", "--help"])
            self.assertIn("-i", r.stdout)

    def test_no_args_prints_help(self):
        with isolated_home() as env:
            r = env.run_cli([])
            # Either prints help (exit 0 or 1) or an explicit "no command" error.
            self.assertIn("usage", r.stdout.lower() + r.stderr.lower())

    def test_unknown_subcommand_errors(self):
        with isolated_home() as env:
            r = env.run_cli(["bogus-subcommand"])
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
