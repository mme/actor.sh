"""e2e TUI: actor tree shows status indicators."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import wait_for_actor_in_tree, watch_app


class StatusIconTests(unittest.IsolatedAsyncioTestCase):

    async def test_done_actor_appears_in_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "task"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "alice")

    async def test_error_actor_appears_in_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "task"],
                        **claude_responds("oops", exit=2))
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "alice")

    async def test_idle_actor_appears_in_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "alice")


if __name__ == "__main__":
    unittest.main()
