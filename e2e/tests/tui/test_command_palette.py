"""e2e TUI: command palette + help overlay."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class CommandPaletteTests(unittest.IsolatedAsyncioTestCase):

    async def test_p_opens_palette(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.2)
                # CommandPalette mounts as a SystemModalScreen.
                from textual.screen import SystemModalScreen
                self.assertIsInstance(app.screen, SystemModalScreen)

    async def test_action_show_help_panel_opens_overlay(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.2)
                from actor.watch.help_overlay import HelpOverlay
                self.assertIsInstance(app.screen, HelpOverlay)

    async def test_help_dismisses_with_escape(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.2)
                await pilot.press("escape")
                await pilot.pause(0.2)
                from actor.watch.help_overlay import HelpOverlay
                self.assertNotIsInstance(app.screen, HelpOverlay)


if __name__ == "__main__":
    unittest.main()
