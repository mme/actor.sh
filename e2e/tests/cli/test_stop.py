"""e2e: `actor stop` — interrupt a running actor."""
from __future__ import annotations

import time
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorStopTests(unittest.TestCase):

    def test_stop_running_actor(self):
        with isolated_home() as env:
            # Spawn a long-sleeping fake claude in the background. The CLI
            # blocks until completion, so we need to stop from a sibling
            # process — simulate by spawning via shell + backgrounding.
            import subprocess
            p = subprocess.Popen(
                ["actor", "new", "alice", "do x"],
                env=env.env(**claude_responds("ok", sleep=10)),
                cwd=str(env.cwd),
            )
            try:
                # Give it time to start.
                time.sleep(0.5)
                r = env.run_cli(["stop", "alice"])
                self.assertEqual(r.returncode, 0, msg=r.stderr)
                p.wait(timeout=5)
            finally:
                if p.poll() is None:
                    p.kill()
            actor = env.fetch_actor("alice")
            # After stop, the most recent run should be STOPPED or ERROR.
            with env.db() as db:
                run = db.latest_run("alice")
                self.assertIn(run.status.as_str(), ("stopped", "error"))

    def test_stop_idle_actor_is_noop(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["stop", "alice"])
            # Spec: stop on idle should be a clean no-op or a clear message.
            # Don't assert exit code; just don't crash hard.
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
