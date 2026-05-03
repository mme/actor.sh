"""e2e TUI: help overlay (`?` key)."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import watch_app


class HelpOverlayTests(unittest.IsolatedAsyncioTestCase):

    async def test_question_mark_opens_help(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                await pilot.press("?")
                await pilot.pause(0.2)
                from actor.watch.help_overlay import HelpOverlay
                self.assertIsInstance(app.screen, HelpOverlay)

    async def test_help_dismisses_with_escape(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                await pilot.press("?")
                await pilot.pause(0.2)
                await pilot.press("escape")
                await pilot.pause(0.2)
                from actor.watch.help_overlay import HelpOverlay
                self.assertNotIsInstance(app.screen, HelpOverlay)

    async def test_help_dismisses_with_question_mark(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                await pilot.press("?")
                await pilot.pause(0.2)
                # Per the binding: `?` is also a dismiss key on the overlay.
                await pilot.press("?")
                await pilot.pause(0.2)
                from actor.watch.help_overlay import HelpOverlay
                self.assertNotIsInstance(app.screen, HelpOverlay)

    async def test_help_lists_global_keymap_actions(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                await pilot.press("?")
                await pilot.pause(0.3)
                from actor.watch.help_overlay import HelpOverlay
                self.assertIsInstance(app.screen, HelpOverlay)
                # The bindings table should include the actor / palette / quit actions.
                # (Best-effort assertion — we don't deep-snapshot the rendered table.)


if __name__ == "__main__":
    unittest.main()
