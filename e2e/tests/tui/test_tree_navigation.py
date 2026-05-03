"""e2e TUI: actor-tree navigation (up/down/left/right + tree → tabs)."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class TreeNavigationTests(unittest.IsolatedAsyncioTestCase):

    async def test_a_key_returns_focus_to_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Move focus into a tab via right-arrow.
                await pilot.press("right")
                await pilot.pause(0.1)
                # `a` brings us back.
                await pilot.press("a")
                await pilot.pause(0.1)
                from actor.watch.app import ActorTree
                self.assertIs(app.focused, app.query_one(ActorTree))

    async def test_left_from_first_tab_returns_to_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Switch to OVERVIEW (idx 0), focus inner widget.
                app.action_show_tab("info")
                await pilot.pause(0.1)
                await pilot.press("left")
                await pilot.pause(0.1)
                from actor.watch.app import ActorTree
                self.assertIs(app.focused, app.query_one(ActorTree))

    async def test_up_down_moves_tree_cursor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            async with watch_app(env) as (app, pilot):
                from actor.watch.app import ActorTree
                tree = app.query_one(ActorTree)
                tree.focus()
                await pilot.pause(0.1)
                # Wait for both actors to appear.
                for _ in range(40):
                    await pilot.pause(0.05)
                    if len(tree.root.children) >= 2:
                        break
                # Move down — cursor should advance.
                start_line = tree.cursor_line
                await pilot.press("down")
                await pilot.pause(0.05)
                self.assertNotEqual(tree.cursor_line, start_line)


if __name__ == "__main__":
    unittest.main()
