"""e2e TUI: actor tree fails to re-sort when statuses change.

`helpers.group_by_parent` defines status + recency ordering for every
tree group. These tests cover both root actors and child actors under
an expanded parent: when the actor set is stable, status or metadata
updates must still reorder the visible rows in that group.
"""
from __future__ import annotations

import subprocess
import time
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import wait_for_actor_in_tree, watch_app


class DiscoveredActorWatchUiFailures(unittest.IsolatedAsyncioTestCase):

    def _find_tree_node(self, node, name: str):
        for child in node.children:
            if child.data and child.data.name == name:
                return child
            hit = self._find_tree_node(child, name)
            if hit is not None:
                return hit
        return None

    def _actor_tree_names(self, app) -> list[str]:
        from actor.watch.app import ActorTree

        tree = app.query_one(ActorTree)
        return [node.data.name for node in tree.root.children if node.data]

    def _child_tree_names(self, app, parent: str) -> list[str]:
        from actor.watch.app import ActorTree

        tree = app.query_one(ActorTree)
        parent_node = self._find_tree_node(tree.root, parent)
        if parent_node is None:
            return []
        return [node.data.name for node in parent_node.children if node.data]

    async def _wait_for_actor_anywhere(self, pilot, app, name: str,
                                       timeout: float = 8.0) -> None:
        from actor.watch.app import ActorTree

        for _ in range(int(timeout / 0.05)):
            await pilot.pause(0.05)
            tree = app.query_one(ActorTree)
            if self._find_tree_node(tree.root, name) is not None:
                return
        raise AssertionError(f"actor {name!r} never appeared in the tree")

    async def test_actor_tree_reorders_when_running_actor_finishes(self):
        with isolated_home() as env:
            env.run_cli(["new", "bravo"])
            proc = subprocess.Popen(
                ["actor", "new", "golf", "short task"],
                env=env.env(**claude_responds("ok", sleep=5.0)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.2)
                async with watch_app(env, size=(100, 30)) as (app, pilot):
                    await wait_for_actor_in_tree(pilot, app, "golf", timeout=8)
                    await wait_for_actor_in_tree(pilot, app, "bravo", timeout=8)
                    # Wait for the watch app to actually observe golf
                    # as RUNNING — sleep=5 gives a wide window, but on
                    # a slow CI the first poll could otherwise land
                    # before claude's row is alive.
                    for _ in range(80):
                        await pilot.pause(0.1)
                        status = app._prev_statuses.get("golf")
                        if status is not None and status.value == "running":
                            break
                    self.assertEqual(self._actor_tree_names(app)[:2],
                                     ["golf", "bravo"])

                    proc.wait(timeout=10)
                    for _ in range(80):
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

    async def test_actor_tree_reorders_child_when_running_actor_finishes(self):
        with isolated_home() as env:
            env.run_cli(["new", "parent"])
            env.run_cli(["new", "idle-child"], ACTOR_NAME="parent")
            proc = subprocess.Popen(
                ["actor", "new", "running-child", "short task"],
                env=env.env(
                    ACTOR_NAME="parent",
                    **claude_responds("ok", sleep=1.0),
                ),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.2)
                async with watch_app(env, size=(100, 30)) as (app, pilot):
                    for name in ("parent", "idle-child", "running-child"):
                        await self._wait_for_actor_anywhere(pilot, app, name)
                    parent_node = self._find_tree_node(
                        app.query_one("#actor-tree").root, "parent"
                    )
                    parent_node.expand()
                    self.assertEqual(
                        self._child_tree_names(app, "parent")[:2],
                        ["running-child", "idle-child"],
                    )

                    proc.wait(timeout=5)
                    for _ in range(50):
                        await pilot.pause(0.1)
                        status = app._prev_statuses.get("running-child")
                        if status is not None and status.value == "done":
                            break

                    self.assertEqual(
                        self._child_tree_names(app, "parent")[:2],
                        ["idle-child", "running-child"],
                        "child tree keeps a completed child above an idle child",
                    )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    async def test_actor_tree_reorders_child_when_existing_actor_starts_running(self):
        with isolated_home() as env:
            env.run_cli(["new", "parent"])
            env.run_cli(["new", "done-child", "done"],
                        ACTOR_NAME="parent", **claude_responds("ok"))
            env.run_cli(["new", "idle-child"], ACTOR_NAME="parent")
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                for name in ("parent", "done-child", "idle-child"):
                    await self._wait_for_actor_anywhere(pilot, app, name)
                parent_node = self._find_tree_node(
                    app.query_one("#actor-tree").root, "parent"
                )
                parent_node.expand()
                self.assertEqual(
                    self._child_tree_names(app, "parent")[:2],
                    ["idle-child", "done-child"],
                )

                proc = subprocess.Popen(
                    ["actor", "run", "done-child", "again"],
                    env=env.env(**claude_responds("ok", sleep=5)),
                    cwd=str(env.cwd),
                )
                try:
                    time.sleep(0.4)
                    for _ in range(50):
                        await pilot.pause(0.1)
                        status = app._prev_statuses.get("done-child")
                        if status is not None and status.value == "running":
                            break

                    self.assertEqual(
                        self._child_tree_names(app, "parent")[:2],
                        ["done-child", "idle-child"],
                        "child tree leaves a newly running child below idle children",
                    )
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)

    async def test_actor_tree_reorders_child_when_metadata_updates(self):
        with isolated_home() as env:
            env.run_cli(["new", "parent"])
            env.run_cli(["new", "older-child"], ACTOR_NAME="parent")
            time.sleep(0.05)
            env.run_cli(["new", "newer-child"], ACTOR_NAME="parent")
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                for name in ("parent", "older-child", "newer-child"):
                    await self._wait_for_actor_anywhere(pilot, app, name)
                parent_node = self._find_tree_node(
                    app.query_one("#actor-tree").root, "parent"
                )
                parent_node.expand()
                self.assertEqual(
                    self._child_tree_names(app, "parent")[:2],
                    ["newer-child", "older-child"],
                )

                time.sleep(0.05)
                env.run_cli(["config", "older-child", "effort=max"])
                for _ in range(50):
                    await pilot.pause(0.1)
                    node = self._find_tree_node(
                        app.query_one("#actor-tree").root, "older-child"
                    )
                    if (
                        node is not None
                        and node.data.config.agent_args.get("effort") == "max"
                    ):
                        break

                self.assertEqual(
                    self._child_tree_names(app, "parent")[:2],
                    ["older-child", "newer-child"],
                    "child tree does not move the most recently updated child",
                )


if __name__ == "__main__":
    unittest.main()
