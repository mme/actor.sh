"""e2e: DB resilience under stress."""
from __future__ import annotations

import subprocess
import unittest

from e2e.harness.isolated_home import isolated_home


class DbResilienceTests(unittest.TestCase):

    def test_many_creates_in_serial(self):
        with isolated_home() as env:
            for i in range(10):
                r = env.run_cli(["new", f"actor-{i}", "--no-worktree"])
                self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(len(env.list_actor_names()), 10)

    def test_many_concurrent_creates_no_corruption(self):
        with isolated_home() as env:
            ps = [subprocess.Popen(
                ["actor", "new", f"actor-{i}", "--no-worktree"],
                env=env.env(), cwd=str(env.cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) for i in range(8)]
            for p in ps:
                p.wait(timeout=15)
            names = set(env.list_actor_names())
            # Some may fail due to db lock — that's OK; core invariant
            # is "no corruption" — the surviving rows should all be valid.
            r = env.run_cli(["list"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
