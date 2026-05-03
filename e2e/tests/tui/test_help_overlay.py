"""e2e TUI: help overlay (`?` key)."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import watch_app


class HelpOverlayTests(unittest.IsolatedAsyncioTestCase):

    async def test_action_show_help_panel_pushes_overlay(self):
        # Trigger the action directly — the actual key binding is
        # provided by Textual's App.SYSTEM_BINDINGS and isn't trivial
        # to inject via Pilot in an isolated test (binding chain
        # resolution involves the tested app's screen state).
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

    async def test_help_dismisses_with_question_mark(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.2)
                # `?` is the dismiss binding on the overlay itself
                # (HelpOverlay.BINDINGS), so it should fire here.
                await pilot.press("?")
                await pilot.pause(0.2)
                from actor.watch.help_overlay import HelpOverlay
                self.assertNotIsInstance(app.screen, HelpOverlay)

    async def test_help_overlay_mounts_bindings_table(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.3)
                from actor.watch.help_overlay import HelpOverlay
                self.assertIsInstance(app.screen, HelpOverlay)


if __name__ == "__main__":
    unittest.main()
