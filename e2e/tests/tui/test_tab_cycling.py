"""e2e TUI: tab key bindings + cycling."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class TabCyclingTests(unittest.IsolatedAsyncioTestCase):

    async def test_o_selects_overview_tab(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("o")
                await pilot.pause(0.1)
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabs.active, "info")

    async def test_d_selects_diff_tab(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("d")
                await pilot.pause(0.1)
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabs.active, "diff")

    async def test_right_from_diff_stays_on_diff(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("diff")
                await pilot.pause(0.1)
                await pilot.press("right")
                await pilot.pause(0.1)
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabs.active, "diff")


if __name__ == "__main__":
    unittest.main()
