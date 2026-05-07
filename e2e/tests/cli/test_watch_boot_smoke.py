"""Smoke test: `actor watch` boots far enough that its asyncio loop
is set up correctly.

Regression guard for a bug that landed in main: the async migration
(PR #88) wrapped `cli.main` in `asyncio.run`, but `run_watch` then
called Textual's sync `App.run()`, which itself calls `asyncio.run`.
Double-nested asyncio.run raises:

    RuntimeError: asyncio.run() cannot be called from a running event loop

The bug only manifested when the CLI binary was actually invoked —
existing watch tests use Pilot's `App.run_test()` harness, which
embeds the app in the test's event loop and bypasses `App.run()`.
This test starts a real subprocess; if anything similar regresses,
the traceback will appear in stderr.
"""
from __future__ import annotations

import shutil
import signal
import subprocess
import time
import unittest
from pathlib import Path

from e2e.harness.isolated_home import isolated_home


def _resolve_actor_bin() -> str:
    return shutil.which("actor") or "actor"


class WatchBootSmokeTests(unittest.TestCase):

    def test_watch_boot_does_not_crash_with_nested_asyncio_run(self):
        """Spawn `actor watch --no-animation` and let it run briefly.

        The asyncio.run() nesting bug fires during boot, before any
        TTY/terminal interaction matters — so we can spawn headlessly
        with stdin/stdout/stderr piped, give it a moment, then send
        SIGTERM. The only thing we assert is that stderr does not
        contain the specific traceback signature of the regression.
        """
        with isolated_home() as env:
            proc = subprocess.Popen(
                [_resolve_actor_bin(), "watch", "--no-animation"],
                env=env.env(),
                cwd=str(env.cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # 1.5s is enough for the asyncio.run nesting bug to fire
                # if it's there — the crash happens in App.run() before
                # any UI work. Healthy boot stays alive past this.
                time.sleep(1.5)
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                self.fail("watch did not exit after SIGTERM")

            stderr_text = stderr.decode(errors="replace")
            # Regression-specific assertion. We don't care about other
            # boot warnings (Textual headless mode complaints, missing
            # TTY noise, etc.) — only this exact bug class.
            self.assertNotIn(
                "asyncio.run() cannot be called from a running event loop",
                stderr_text,
                msg=(
                    "watch crashed with nested asyncio.run() — "
                    "regression of the PR #88 / #94-era bug. "
                    f"stderr was:\n{stderr_text}"
                ),
            )
            self.assertNotIn(
                "RuntimeWarning: coroutine 'App.run.<locals>.run_app' was never awaited",
                stderr_text,
                msg=f"watch boot left an unawaited coroutine. stderr:\n{stderr_text}",
            )


if __name__ == "__main__":
    unittest.main()
