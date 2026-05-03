"""e2e TUI: DIFF pane behavior across worktree states."""
from __future__ import annotations

import unittest
from pathlib import Path

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class DiffPaneStateTests(unittest.IsolatedAsyncioTestCase):

    async def test_diff_pane_for_idle_actor_doesnt_crash(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("diff")
                await pilot.pause(0.5)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_diff_pane_for_no_worktree_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-worktree"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("diff")
                await pilot.pause(0.5)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_diff_pane_with_dirty_worktree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            (Path(actor.dir) / "README.md").write_text("modified content here\n")
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("diff")
                # Wait for diff cycle.
                for _ in range(60):
                    await pilot.pause(0.1)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_diff_pane_with_added_file(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            (Path(actor.dir) / "newfile.txt").write_text("new\n")
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("diff")
                await pilot.pause(2.0)
                self.assertIsNone(getattr(app, "_exception", None))


if __name__ == "__main__":
    unittest.main()
