"""e2e TUI: `q` quits the app cleanly."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import watch_app


class QuitTests(unittest.IsolatedAsyncioTestCase):

    async def test_q_quits_the_app(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await pilot.press("q")
                # After q, the app should be exiting.
                await pilot.pause(0.1)
                self.assertTrue(
                    app._exit or getattr(app, "is_running", True) is False
                )


if __name__ == "__main__":
    unittest.main()
