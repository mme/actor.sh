"""e2e TUI: tree updates when actors are added/removed externally."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import wait_for_actor_in_tree, watch_app


class TreeStateChangeTests(unittest.IsolatedAsyncioTestCase):

    async def test_tree_picks_up_externally_discarded_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "alice")
                env.run_cli(["discard", "alice", "--force"])
                # Wait for poll cycle.
                from actor.watch.app import ActorTree
                for _ in range(40):
                    await pilot.pause(0.1)
                    tree = app.query_one(ActorTree)
                    names = [str(n.label) for n in tree.root.children]
                    if not any("alice" in n for n in names):
                        break
                tree = app.query_one(ActorTree)
                names = [str(n.label) for n in tree.root.children]
                self.assertFalse(any("alice" in n for n in names),
                                 f"alice should be gone; names={names}")

    async def test_tree_initial_load_includes_existing_actors(self):
        with isolated_home() as env:
            for n in ("alpha", "beta", "gamma"):
                env.run_cli(["new", n])
            async with watch_app(env) as (app, pilot):
                for n in ("alpha", "beta", "gamma"):
                    await wait_for_actor_in_tree(pilot, app, n)


if __name__ == "__main__":
    unittest.main()
