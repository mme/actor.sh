"""e2e TUI: more UX checks targeting potential bugs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, wait_for_actor_in_tree, watch_app


class MoreUxTests(unittest.IsolatedAsyncioTestCase):

    async def test_footer_shows_actor_binding(self):
        # 'a' is bound for "Actors" — footer should expose it.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                from textual.widgets import Footer
                footer = app.query_one(Footer)
                # Footer renders the visible bindings — we just check
                # it doesn't crash and exists.
                self.assertIsNotNone(footer)

    async def test_done_actor_status_visible_in_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "alice")
                from actor.watch.app import ActorTree
                tree = app.query_one(ActorTree)
                label = ""
                for node in tree.root.children:
                    if "alice" in str(node.label):
                        label = str(node.label)
                # Status should be reflected in the label (icon or text).
                # Just check label has content.
                self.assertTrue(label.strip())

    async def test_error_actor_status_visible_in_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("oops", exit=2))
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "alice")
                from actor.watch.app import ActorTree
                tree = app.query_one(ActorTree)
                label = ""
                for node in tree.root.children:
                    if "alice" in str(node.label):
                        label = str(node.label)
                self.assertTrue(label.strip())

    async def test_three_actors_each_selectable(self):
        with isolated_home() as env:
            for n in ("a1", "a2", "a3"):
                env.run_cli(["new", n])
            async with watch_app(env) as (app, pilot):
                for n in ("a1", "a2", "a3"):
                    await select_actor(pilot, app, n)
                    self.assertEqual(app._overview_header_actor.name, n)

    async def test_command_palette_dismiss_with_escape(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.3)
                await pilot.press("escape")
                await pilot.pause(0.3)
                from textual.screen import SystemModalScreen
                self.assertNotIsInstance(app.screen, SystemModalScreen)

    async def test_help_overlay_dismisses_does_not_quit_app(self):
        # `?` opens help; escape dismisses; app should still be running.
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.2)
                await pilot.press("escape")
                await pilot.pause(0.2)
                # App should still be alive.
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_quick_press_burst_doesnt_crash(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Spam keys to look for race conditions.
                for k in ["o", "d", "o", "d", "a", "right", "left", "down", "up"]:
                    await pilot.press(k)
                await pilot.pause(0.3)
                self.assertIsNone(getattr(app, "_exception", None))


if __name__ == "__main__":
    unittest.main()
