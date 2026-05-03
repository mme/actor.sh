"""e2e TUI: keybinding invariants — every documented binding does what it says."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class KeybindingTests(unittest.IsolatedAsyncioTestCase):

    async def test_p_opens_palette(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.3)
                from textual.screen import SystemModalScreen
                self.assertIsInstance(app.screen, SystemModalScreen)

    async def test_a_focuses_actor_tree_from_anywhere(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("d")  # focus diff tab
                await pilot.pause(0.1)
                await pilot.press("a")
                await pilot.pause(0.2)
                from actor.watch.app import ActorTree
                self.assertIs(app.focused, app.query_one(ActorTree))

    async def test_o_selects_overview_from_diff(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("d")
                await pilot.pause(0.1)
                await pilot.press("o")
                await pilot.pause(0.1)
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabs.active, "info")

    async def test_d_selects_diff_from_overview(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("o")
                await pilot.pause(0.1)
                await pilot.press("d")
                await pilot.pause(0.1)
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabs.active, "diff")

    async def test_left_arrow_collapses_node_when_tree_focused(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                from actor.watch.app import ActorTree
                tree = app.query_one(ActorTree)
                tree.focus()
                await pilot.pause(0.1)
                await pilot.press("left")
                await pilot.pause(0.1)
                # No crash; focus stays on tree.
                self.assertIs(app.focused, tree)


if __name__ == "__main__":
    unittest.main()
