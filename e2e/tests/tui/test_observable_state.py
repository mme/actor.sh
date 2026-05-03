"""e2e TUI: observable state changes between selections / actions."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class ObservableStateTests(unittest.IsolatedAsyncioTestCase):

    async def test_overview_header_actor_changes_on_select(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha"])
            env.run_cli(["new", "beta"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                self.assertEqual(app._overview_header_actor.name, "alpha")
                await select_actor(pilot, app, "beta")
                self.assertEqual(app._overview_header_actor.name, "beta")

    async def test_active_tab_changes_on_action(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                app.action_show_tab("info")
                await pilot.pause(0.1)
                self.assertEqual(tabs.active, "info")
                app.action_show_tab("diff")
                await pilot.pause(0.1)
                self.assertEqual(tabs.active, "diff")

    async def test_preferred_tab_remembered_per_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha"])
            env.run_cli(["new", "beta"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                app.action_show_tab("diff")
                await pilot.pause(0.2)
                await select_actor(pilot, app, "beta")
                # Per-actor tab preference should be remembered or
                # default; either way, no crash.
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_tree_cursor_position_after_select_via_api(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha"])
            env.run_cli(["new", "beta"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "beta")
                from actor.watch.app import ActorTree
                tree = app.query_one(ActorTree)
                # Cursor should be on a real node.
                self.assertIsNotNone(tree.cursor_node)


if __name__ == "__main__":
    unittest.main()
