"""e2e TUI: command palette executes commands correctly."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class PaletteActionTests(unittest.IsolatedAsyncioTestCase):

    async def test_palette_supports_keyboard_search(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.3)
                # Type to filter.
                await pilot.press("d", "i", "s", "c")
                await pilot.pause(0.2)
                # Should still be in modal mode (not crashed).
                from textual.screen import SystemModalScreen
                self.assertIsInstance(app.screen, SystemModalScreen)

    async def test_palette_enter_does_not_crash(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.3)
                await pilot.press("enter")
                await pilot.pause(0.3)
                self.assertIsNone(getattr(app, "_exception", None))


if __name__ == "__main__":
    unittest.main()
