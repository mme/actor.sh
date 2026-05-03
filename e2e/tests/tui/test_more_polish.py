"""e2e TUI: more polish probes."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class MorePolishTests(unittest.IsolatedAsyncioTestCase):

    async def test_overview_renders_for_actor_with_multiple_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            env.run_cli(["run", "alice", "third"], **claude_responds("c"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Allow render to settle.
                for _ in range(20):
                    await pilot.pause(0.1)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_select_actor_after_external_run_works(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                # Run externally.
                env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
                # Wait + select.
                for _ in range(30):
                    await pilot.pause(0.1)
                await select_actor(pilot, app, "alice")
                self.assertEqual(app._overview_header_actor.name, "alice")

    async def test_app_with_no_actors_shows_splash_or_empty_state(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                # Either splash or empty tree — never crash.
                await pilot.pause(1.0)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_app_recovers_from_actor_added_during_focus(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                # Add another actor — focus might shift unexpectedly.
                env.run_cli(["new", "beta"])
                # Wait for poll cycle.
                for _ in range(40):
                    await pilot.pause(0.1)
                self.assertIsNone(getattr(app, "_exception", None))


if __name__ == "__main__":
    unittest.main()
