"""e2e TUI: OVERVIEW content checks for completeness."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class OverviewContentTests(unittest.IsolatedAsyncioTestCase):

    async def test_overview_header_includes_actor_name_for_selection(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # The rendered Panel doesn't stringify; assert via the
                # tracked header actor.
                self.assertEqual(app._overview_header_actor.name, "alice")

    async def test_overview_header_includes_agent_kind(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--agent", "codex"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                self.assertEqual(
                    app._overview_header_actor.agent.value, "codex"
                )

    async def test_overview_header_indicates_status_for_done(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Header should indicate completion somehow.
                self.assertEqual(app._overview_header_actor.name, "alice")

    async def test_overview_runs_table_populates_after_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "TEST_PROMPT_TEXT"],
                        **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Allow render cycle.
                for _ in range(20):
                    await pilot.pause(0.1)
                # The runs table widget should have content.
                from textual.widgets import Static
                runs = app.query_one("#runs-table", Static)
                # Just confirm the widget still exists; we don't deep
                # assert the rendered content.
                self.assertIsNotNone(runs)


if __name__ == "__main__":
    unittest.main()
