"""e2e workflow: concurrent CLI calls don't corrupt the DB."""
from __future__ import annotations

import subprocess
import unittest

from e2e.harness.isolated_home import isolated_home


class ConcurrentCliTests(unittest.TestCase):

    def test_two_simultaneous_actor_creates(self):
        with isolated_home() as env:
            # Spawn both processes back to back; they race on the DB.
            ps = []
            for n in ("alice", "bob"):
                ps.append(subprocess.Popen(
                    ["actor", "new", n, "--no-worktree"],
                    env=env.env(),
                    cwd=str(env.cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                ))
            for p in ps:
                p.wait(timeout=10)
            # Both should succeed; DB consistent.
            self.assertEqual(set(env.list_actor_names()), {"alice", "bob"})

    def test_list_during_concurrent_creates_does_not_crash(self):
        with isolated_home() as env:
            ps = [subprocess.Popen(
                ["actor", "new", f"a{i}", "--no-worktree"],
                env=env.env(), cwd=str(env.cwd),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            ) for i in range(3)]
            try:
                r = env.run_cli(["list"])
                self.assertEqual(r.returncode, 0, msg=r.stderr)
            finally:
                for p in ps:
                    p.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
