"""e2e: `actor list --status` filter coverage."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorListFilterTests(unittest.TestCase):

    def test_filter_done_only(self):
        with isolated_home() as env:
            env.run_cli(["new", "done-actor", "task"], **claude_responds("ok"))
            env.run_cli(["new", "idle-actor"])
            r = env.run_cli(["list", "--status", "done"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("done-actor", r.stdout)
            self.assertNotIn("idle-actor", r.stdout)

    def test_filter_idle_only(self):
        with isolated_home() as env:
            env.run_cli(["new", "done-actor", "task"], **claude_responds("ok"))
            env.run_cli(["new", "idle-actor"])
            r = env.run_cli(["list", "--status", "idle"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("idle-actor", r.stdout)
            self.assertNotIn("done-actor", r.stdout)

    def test_filter_invalid_status(self):
        with isolated_home() as env:
            r = env.run_cli(["list", "--status", "bogus"])
            # Should error or return empty; never crash.
            self.assertNotIn("Traceback", r.stderr)

    def test_filter_error_status(self):
        with isolated_home() as env:
            env.run_cli(["new", "ok-actor", "task"], **claude_responds("ok"))
            env.run_cli(["new", "err-actor", "task"],
                        **claude_responds("oops", exit=2))
            r = env.run_cli(["list", "--status", "error"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("err-actor", r.stdout)
            self.assertNotIn("ok-actor", r.stdout)


if __name__ == "__main__":
    unittest.main()
