"""e2e TUI: splash screen → main view transition."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import watch_app


class SplashTests(unittest.IsolatedAsyncioTestCase):

    async def test_splash_dismisses_with_animate_false(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                # watch_app already waits for splash to clear.
                self.assertFalse(getattr(app, "_splash_active", True))

    async def test_main_view_shows_actor_panel(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                # ACTOR.SH label should be visible.
                from textual.widgets import Static
                labels = list(app.query("#actors-label"))
                self.assertTrue(labels, "actors-label widget should exist")

    async def test_initial_focus_on_tree(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                from actor.watch.app import ActorTree
                # Allow polling cycle to settle initial focus.
                for _ in range(20):
                    await pilot.pause(0.05)
                    tree = app.query_one(ActorTree)
                    if app.focused is tree:
                        break
                self.assertIs(app.focused, tree)


if __name__ == "__main__":
    unittest.main()
