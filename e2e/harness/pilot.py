"""Pilot bootstrap helpers for `actor watch` e2e tests.

Provides the same harness shape as `tests/test_watch_navigation.py`
but factored out so every TUI test in `e2e/tests/tui/` can reuse it.

Usage:

    class MyTest(unittest.IsolatedAsyncioTestCase):
        async def test_x(self):
            with isolated_home() as env:
                async with watch_app(env) as (app, pilot):
                    await pilot.press("a")
                    await pilot.pause(0.05)
                    self.assertIs(app.focused, app.query_one(ActorTree))
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import patch


@asynccontextmanager
async def watch_app(env, *, size=(120, 40)) -> AsyncIterator[tuple]:
    """Boot ActorWatchApp under Pilot with the given IsolatedHome env.

    Patches HOME for the duration of the context so the app reads the
    test's DB / settings.kdl. Returns (app, pilot) — both queryable
    while the context is open.
    """
    env_patch = patch.dict(os.environ, {"HOME": str(env.home)})
    env_patch.start()
    try:
        from actor.watch.app import ActorWatchApp
        app = ActorWatchApp(animate=False)
        async with app.run_test(size=size) as pilot:
            # Let the splash settle (animate=False but mount still
            # has to flush a frame or two).
            for _ in range(20):
                await pilot.pause(0.05)
                if not getattr(app, "_splash_active", False):
                    break
            yield app, pilot
    finally:
        env_patch.stop()


async def wait_for_actor_in_tree(pilot, app, name: str,
                                 timeout: float = 2.0) -> None:
    """Poll until the named actor shows up in the tree (the watch app's
    poll cycle inserts it on the first refresh)."""
    from actor.watch.app import ActorTree
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        tree = app.query_one(ActorTree)
        for node in tree.root.children:
            if name in str(node.label):
                return
    raise AssertionError(f"actor {name!r} never appeared in the tree")


async def select_actor(pilot, app, name: str) -> None:
    """Move the tree cursor onto the actor's row.

    Uses `move_cursor` (highlight-only) rather than `select_node`
    (which fires NodeSelected → action_enter_interactive → switches
    the detail tab to Interactive and collapses the OVERVIEW log
    widget to width 0). Highlighting is what triggers
    `_refresh_detail`, which is what tests actually want.
    """
    from actor.watch.app import ActorTree
    await wait_for_actor_in_tree(pilot, app, name)
    tree = app.query_one(ActorTree)
    tree.focus()
    for node in tree.root.children:
        if name in str(node.label):
            tree.move_cursor(node)
            break
    await pilot.pause(0.1)


async def wait_for_status(pilot, app, name: str, status_label: str,
                          timeout: float = 5.0) -> None:
    """Poll the tree until the actor's row shows the given status string
    (e.g. 'done', 'running', 'error')."""
    from actor.watch.app import ActorTree
    for _ in range(int(timeout / 0.1)):
        await pilot.pause(0.1)
        tree = app.query_one(ActorTree)
        for node in tree.root.children:
            label = str(node.label)
            if name in label and status_label.lower() in label.lower():
                return
    raise AssertionError(
        f"actor {name!r} never reached status {status_label!r} in the tree"
    )
