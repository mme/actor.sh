"""e2e: `actor config` view formatting."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class ConfigViewFormatTests(unittest.TestCase):

    def test_config_view_shows_all_keys(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus",
                         "--config", "effort=max"])
            r = env.run_cli(["config", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("model", r.stdout)
            self.assertIn("opus", r.stdout)
            self.assertIn("effort", r.stdout)
            self.assertIn("max", r.stdout)

    def test_config_view_includes_actor_keys(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-use-subscription"])
            r = env.run_cli(["config", "alice"])
            self.assertIn("use-subscription", r.stdout)

    def test_config_view_no_trailing_traceback(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["config", "alice"])
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
