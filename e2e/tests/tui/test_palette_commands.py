"""e2e TUI: command palette specific commands."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class PaletteCommandsTests(unittest.IsolatedAsyncioTestCase):

    async def test_palette_offers_stop_command(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.3)
                await pilot.press("s", "t", "o", "p")
                await pilot.pause(0.3)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_palette_offers_discard_command(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.3)
                await pilot.press("d", "i", "s", "c", "a", "r", "d")
                await pilot.pause(0.3)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_palette_dismisses_with_escape(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.2)
                await pilot.press("escape")
                await pilot.pause(0.2)
                from textual.screen import SystemModalScreen
                self.assertNotIsInstance(app.screen, SystemModalScreen)


if __name__ == "__main__":
    unittest.main()
