"""e2e TUI: splash screen → main view transition."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import watch_app


class SplashTests(unittest.IsolatedAsyncioTestCase):

    async def test_splash_dismisses_when_actors_exist(self):
        # The splash stays active while the DB has zero actors (it's the
        # "you have nothing yet, set up an actor" affordance). Add one
        # so the main view should take over.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                # Allow the poll cycle to fetch the actor and flip splash off.
                for _ in range(40):
                    await pilot.pause(0.1)
                    if not getattr(app, "_splash_active", True):
                        break
                self.assertFalse(getattr(app, "_splash_active", True))

    async def test_splash_remains_when_no_actors(self):
        # Empty DB → splash stays. This is the "first run" UX.
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                await pilot.pause(0.5)
                self.assertTrue(getattr(app, "_splash_active", False))

    async def test_main_view_shows_actor_panel(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                labels = list(app.query("#actors-label"))
                self.assertTrue(labels, "actors-label widget should exist")

    async def test_initial_focus_on_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                from actor.watch.app import ActorTree
                for _ in range(40):
                    await pilot.pause(0.1)
                    tree = app.query_one(ActorTree)
                    if app.focused is tree:
                        break
                self.assertIs(app.focused, tree)


if __name__ == "__main__":
    unittest.main()
