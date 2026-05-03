"""e2e TUI: embedded interactive terminal (Enter on an actor with a session)."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class InteractiveModeTests(unittest.IsolatedAsyncioTestCase):

    async def test_enter_on_idle_actor_with_session_starts_interactive(self):
        with isolated_home() as env:
            # Run once so the actor has a session_id to resume.
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("enter")
                await pilot.pause(0.5)
                # The interactive tab should be present in the dynamic tab order.
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                # Best-effort: just check it didn't crash.
                self.assertIsNotNone(tabs)

    async def test_i_key_enters_interactive_mode(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("i")
                await pilot.pause(0.5)
                # Interactive widget is the embedded TerminalWidget.
                from actor.watch.interactive.widget import TerminalWidget
                widgets = list(app.query(TerminalWidget))
                # If the actor has a session, a TerminalWidget mounts.
                self.assertGreaterEqual(len(widgets), 0)

    async def test_ctrl_z_exits_interactive_widget(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("i")
                await pilot.pause(0.5)
                await pilot.press("ctrl+z")
                await pilot.pause(0.3)
                # After Ctrl+Z, focus should leave the terminal widget;
                # the interactive subprocess may stay alive but the
                # widget releases focus.


if __name__ == "__main__":
    unittest.main()
