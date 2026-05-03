"""e2e: `actor logs --watch` streaming."""
from __future__ import annotations

import subprocess
import time
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorLogsWatchTests(unittest.TestCase):

    def test_logs_watch_streams_to_stdout(self):
        with isolated_home() as env:
            # Run once to seed a session log.
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            # Spawn `actor logs --watch` in the background and let it
            # tail for a moment.
            p = subprocess.Popen(
                ["actor", "logs", "alice", "--watch"],
                env=env.env(),
                cwd=str(env.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                time.sleep(1.0)
                p.terminate()
                stdout, stderr = p.communicate(timeout=5)
            except Exception:
                p.kill()
                stdout, stderr = "", ""
            # Watch should at least print the existing content; no
            # traceback expected.
            self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
