"""e2e TUI: OVERVIEW pane behavior across actor states."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class OverviewStateTests(unittest.IsolatedAsyncioTestCase):

    async def test_overview_for_done_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                self.assertIsNone(getattr(app, "_exception", None))
                self.assertEqual(app._overview_header_actor.name, "alice")

    async def test_overview_for_error_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("oops", exit=2))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_overview_when_polling_refreshes_during_select(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Let several poll cycles run while focused.
                for _ in range(40):
                    await pilot.pause(0.1)
                self.assertIsNone(getattr(app, "_exception", None))


if __name__ == "__main__":
    unittest.main()
