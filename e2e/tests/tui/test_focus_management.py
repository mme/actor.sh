"""e2e TUI: focus management edge cases."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class FocusManagementTests(unittest.IsolatedAsyncioTestCase):

    async def test_focus_starts_on_tree_after_splash(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                from actor.watch.app import ActorTree
                # Wait until focus settles.
                for _ in range(30):
                    await pilot.pause(0.1)
                    if app.focused is app.query_one(ActorTree):
                        break
                self.assertIs(app.focused, app.query_one(ActorTree))

    async def test_p_then_escape_returns_focus_to_previous(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                from actor.watch.app import ActorTree
                tree = app.query_one(ActorTree)
                self.assertIs(app.focused, tree)
                # Open palette.
                await pilot.press("p")
                await pilot.pause(0.2)
                # Dismiss.
                await pilot.press("escape")
                await pilot.pause(0.3)
                # Focus should be back on the tree.
                self.assertIs(app.focused, tree)

    async def test_help_then_escape_returns_focus(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                from actor.watch.app import ActorTree
                tree = app.query_one(ActorTree)
                app.action_show_help_panel()
                await pilot.pause(0.2)
                await pilot.press("escape")
                await pilot.pause(0.3)
                self.assertIs(app.focused, tree)

    async def test_focus_after_tab_change_lands_in_tab(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("info")
                await pilot.pause(0.2)
                # Focus should be in the OVERVIEW tab content.
                from textual.widgets import RichLog
                log = app.query_one("#logs-content", RichLog)
                self.assertIs(app.focused, log)


if __name__ == "__main__":
    unittest.main()
