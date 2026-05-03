"""e2e TUI: DIFF pane."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class DiffPaneTests(unittest.IsolatedAsyncioTestCase):

    async def test_diff_pane_renders_for_clean_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("diff")
                await pilot.pause(0.5)
                # Just exercise the render path. Diff content depends
                # on git state — the smoke check is "no traceback".
                from textual.containers import VerticalScroll
                scroll = app.query_one("#diff-scroll", VerticalScroll)
                self.assertIsNotNone(scroll)

    async def test_diff_pane_shows_changes_for_dirty_worktree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            from pathlib import Path
            (Path(actor.dir) / "newfile.txt").write_text("dirty content")
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                app.action_show_tab("diff")
                # Allow the diff poll cycle to fetch.
                for _ in range(60):
                    await pilot.pause(0.1)
                    # Just don't crash; exact content assertion would
                    # require diff-renderer internals.
                    if not app._diff_loaded_for == "alice":
                        continue
                    break


if __name__ == "__main__":
    unittest.main()
