"""e2e TUI: rendering with multiple actors in different states."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, wait_for_actor_in_tree, watch_app


class MultiActorStateTests(unittest.IsolatedAsyncioTestCase):

    async def test_three_actors_all_visible(self):
        with isolated_home() as env:
            for n in ("alice", "bob", "carol"):
                env.run_cli(["new", n])
            async with watch_app(env) as (app, pilot):
                for n in ("alice", "bob", "carol"):
                    await wait_for_actor_in_tree(pilot, app, n)

    async def test_selecting_each_actor_updates_overview(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                self.assertEqual(app._overview_header_actor.name, "alice")
                await select_actor(pilot, app, "bob")
                self.assertEqual(app._overview_header_actor.name, "bob")


if __name__ == "__main__":
    unittest.main()
