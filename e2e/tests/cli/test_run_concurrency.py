"""e2e: concurrent runs / stops on the same actor."""
from __future__ import annotations

import subprocess
import time
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RunConcurrencyTests(unittest.TestCase):

    def test_run_then_immediately_stop(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            p = subprocess.Popen(
                ["actor", "run", "alice", "long task"],
                env=env.env(**claude_responds("ok", sleep=5)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                r = env.run_cli(["stop", "alice"])
                # Stop should succeed even mid-run.
                self.assertEqual(r.returncode, 0, msg=r.stderr)
                p.wait(timeout=10)
            finally:
                if p.poll() is None:
                    p.kill()

    def test_run_then_stop_then_run_again(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            p = subprocess.Popen(
                ["actor", "run", "alice", "long task"],
                env=env.env(**claude_responds("ok", sleep=3)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                env.run_cli(["stop", "alice"])
                p.wait(timeout=10)
            finally:
                if p.poll() is None:
                    p.kill()
            # After stop, a new run should be possible.
            r = env.run_cli(["run", "alice", "second try"],
                            **claude_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
