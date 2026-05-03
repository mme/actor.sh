"""e2e: idempotent operations should be idempotent."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class IdempotencyTests(unittest.TestCase):

    def test_setup_twice_is_safe(self):
        with isolated_home() as env:
            r1 = env.run_cli(["setup", "--for", "claude-code"])
            r2 = env.run_cli(["setup", "--for", "claude-code"])
            # Both should succeed; second is a no-op or refresh.
            self.assertNotIn("Traceback", r2.stderr)

    def test_list_twice_returns_same(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r1 = env.run_cli(["list"])
            r2 = env.run_cli(["list"])
            self.assertEqual(r1.stdout, r2.stdout)

    def test_show_twice_returns_same(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r1 = env.run_cli(["show", "alice"])
            r2 = env.run_cli(["show", "alice"])
            self.assertEqual(r1.stdout, r2.stdout)

    def test_config_view_twice_returns_same(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            r1 = env.run_cli(["config", "alice"])
            r2 = env.run_cli(["config", "alice"])
            self.assertEqual(r1.stdout, r2.stdout)


if __name__ == "__main__":
    unittest.main()
