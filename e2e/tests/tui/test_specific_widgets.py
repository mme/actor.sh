"""e2e TUI: specific widget behavior assertions."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class SpecificWidgetTests(unittest.IsolatedAsyncioTestCase):

    async def test_overview_pane_has_last_interaction_widget(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                widgets = list(app.query("#overview-last-interaction"))
                self.assertEqual(len(widgets), 1,
                                 "overview-last-interaction widget should exist")

    async def test_actors_underline_widget_exists(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                widgets = list(app.query("#actors-underline"))
                self.assertEqual(len(widgets), 1)

    async def test_tabbed_content_has_two_panes(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                # Should have at least info + diff tabs.
                self.assertGreaterEqual(len(list(tabs.query("TabPane"))), 2)

    async def test_main_layout_horizontal_split(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                from textual.containers import Horizontal
                main = app.query_one("#main-layout", Horizontal)
                self.assertIsNotNone(main)

    async def test_actor_panel_left_side(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                widgets = list(app.query("#actor-panel"))
                self.assertEqual(len(widgets), 1)

    async def test_detail_panel_right_side(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                widgets = list(app.query("#detail-panel"))
                self.assertEqual(len(widgets), 1)


if __name__ == "__main__":
    unittest.main()
