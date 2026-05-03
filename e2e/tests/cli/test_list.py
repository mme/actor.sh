"""e2e: `actor list` — table output and filters."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorListTests(unittest.TestCase):

    def test_list_empty_db_header_only(self):
        with isolated_home() as env:
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn("NAME", lines[0])

    def test_list_shows_multiple_actors(self):
        with isolated_home() as env:
            for n in ("alice", "bob", "carol"):
                env.run_cli(["new", n])
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            for n in ("alice", "bob", "carol"):
                self.assertIn(n, r.stdout)

    def test_list_status_filter(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            env.run_cli(["new", "bob"])  # idle
            r = env.run_cli(["list", "--status", "done"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("alice", r.stdout)
            self.assertNotIn("bob", r.stdout)


if __name__ == "__main__":
    unittest.main()
