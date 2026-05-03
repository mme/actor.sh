"""e2e TUI: actor tree fails to re-sort when statuses change.

`helpers.group_by_parent` defines a status-based ordering (RUNNING <
ERROR < IDLE < DONE < STOPPED), but `ActorTree.update_actors` only
runs the sort when the set of actor names changes. Status transitions
that happen on a stable name set leave the tree's order stale —
finished actors stay above newly-running ones, etc. Each test below
sets up a known-stable name set, triggers a status change, and asserts
the order updated.
"""
from __future__ import annotations

import subprocess
import time
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import wait_for_actor_in_tree, watch_app


class DiscoveredActorWatchUiFailures(unittest.IsolatedAsyncioTestCase):

    def _actor_tree_names(self, app) -> list[str]:
        from actor.watch.app import ActorTree

        tree = app.query_one(ActorTree)
        return [node.data.name for node in tree.root.children if node.data]

    async def test_actor_tree_reorders_when_running_actor_finishes(self):
        with isolated_home() as env:
            env.run_cli(["new", "bravo"])
            proc = subprocess.Popen(
                ["actor", "new", "golf", "short task"],
                env=env.env(**claude_responds("ok", sleep=1.0)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.2)
                async with watch_app(env, size=(100, 30)) as (app, pilot):
                    await wait_for_actor_in_tree(pilot, app, "golf", timeout=8)
                    await wait_for_actor_in_tree(pilot, app, "bravo", timeout=8)
                    self.assertEqual(self._actor_tree_names(app)[:2],
                                     ["golf", "bravo"])

                    proc.wait(timeout=5)
                    for _ in range(50):
                        await pilot.pause(0.1)
                        status = app._prev_statuses.get("golf")
                        if status is not None and status.value == "done":
                            break

                    self.assertEqual(
                        self._actor_tree_names(app)[:2],
                        ["bravo", "golf"],
                        "actor tree keeps a completed actor above an idle actor",
                    )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    async def test_actor_tree_reorders_when_existing_actor_starts_running(self):
        with isolated_home() as env:
            env.run_cli(["new", "hotel", "done"], **claude_responds("ok"))
            env.run_cli(["new", "india"])
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "hotel", timeout=8)
                await wait_for_actor_in_tree(pilot, app, "india", timeout=8)
                self.assertEqual(self._actor_tree_names(app)[:2],
                                 ["india", "hotel"])

                proc = subprocess.Popen(
                    ["actor", "run", "hotel", "again"],
                    env=env.env(**claude_responds("ok", sleep=5)),
                    cwd=str(env.cwd),
                )
                try:
                    time.sleep(0.4)
                    for _ in range(50):
                        await pilot.pause(0.1)
                        status = app._prev_statuses.get("hotel")
                        if status is not None and status.value == "running":
                            break

                    self.assertEqual(
                        self._actor_tree_names(app)[:2],
                        ["hotel", "india"],
                        "actor tree leaves a newly running actor below idle actors",
                    )
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)

    async def test_actor_tree_reorders_when_running_actor_is_stopped(self):
        with isolated_home() as env:
            env.run_cli(["new", "kilo"])
            proc = subprocess.Popen(
                ["actor", "new", "juliet", "long task"],
                env=env.env(**claude_responds("ok", sleep=5)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                async with watch_app(env, size=(100, 30)) as (app, pilot):
                    await wait_for_actor_in_tree(pilot, app, "juliet", timeout=8)
                    await wait_for_actor_in_tree(pilot, app, "kilo", timeout=8)
                    self.assertEqual(self._actor_tree_names(app)[:2],
                                     ["juliet", "kilo"])

                    env.run_cli(["stop", "juliet"], timeout=10)
                    proc.wait(timeout=5)
                    for _ in range(50):
                        await pilot.pause(0.1)
                        status = app._prev_statuses.get("juliet")
                        if status is not None and status.value == "stopped":
                            break

                    self.assertEqual(
                        self._actor_tree_names(app)[:2],
                        ["kilo", "juliet"],
                        "actor tree keeps a stopped actor above an idle actor",
                    )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    async def test_actor_tree_reorders_when_actor_metadata_updates(self):
        with isolated_home() as env:
            env.run_cli(["new", "lima"])
            time.sleep(0.05)
            env.run_cli(["new", "mike"])
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "lima", timeout=8)
                await wait_for_actor_in_tree(pilot, app, "mike", timeout=8)
                self.assertEqual(self._actor_tree_names(app)[:2],
                                 ["mike", "lima"])

                time.sleep(0.05)
                env.run_cli(["config", "lima", "effort=max"])
                for _ in range(50):
                    await pilot.pause(0.1)
                    for node in app.query_one("#actor-tree").root.children:
                        if (
                            node.data
                            and node.data.name == "lima"
                            and node.data.config.agent_args.get("effort") == "max"
                        ):
                            break
                    else:
                        continue
                    break

                self.assertEqual(
                    self._actor_tree_names(app)[:2],
                    ["lima", "mike"],
                    "actor tree does not move the most recently updated actor",
                )


if __name__ == "__main__":
    unittest.main()
