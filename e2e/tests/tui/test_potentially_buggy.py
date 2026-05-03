"""e2e TUI: more probes likely to surface bugs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, wait_for_actor_in_tree, watch_app


class PotentiallyBuggyTests(unittest.IsolatedAsyncioTestCase):

    async def test_q_in_help_overlay_quits_or_dismisses(self):
        # Pressing q while the help overlay is open: either quit OR
        # dismiss — but not error.
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.2)
                await pilot.press("q")
                await pilot.pause(0.3)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_arrows_in_modal_dont_navigate_underlying_tabs(self):
        # When a modal is open, arrow keys should affect the modal,
        # not the tabs underneath.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                tabs.active = "info"
                await pilot.pause(0.1)
                app.action_show_help_panel()
                await pilot.pause(0.2)
                await pilot.press("right")
                await pilot.pause(0.2)
                # Tab should still be "info".
                self.assertEqual(tabs.active, "info")

    async def test_running_actor_overview_updates_periodically(self):
        # Running actor — overview header should periodically refresh.
        # Just check the periodic tick doesn't crash.
        import subprocess, time
        with isolated_home() as env:
            p = subprocess.Popen(
                ["actor", "new", "alice", "long task"],
                env=env.env(**claude_responds("ok", sleep=2)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                async with watch_app(env) as (app, pilot):
                    await select_actor(pilot, app, "alice")
                    for _ in range(20):
                        await pilot.pause(0.1)
                    self.assertIsNone(getattr(app, "_exception", None))
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)

    async def test_repeated_select_same_actor_stable(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                for _ in range(5):
                    await select_actor(pilot, app, "alice")
                self.assertEqual(app._overview_header_actor.name, "alice")

    async def test_actor_appears_in_tree_with_dynamic_actor_added(self):
        # Boot empty (with one actor so splash clears), then add another.
        with isolated_home() as env:
            env.run_cli(["new", "first"])
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "first")
                env.run_cli(["new", "second"])
                await wait_for_actor_in_tree(pilot, app, "second", timeout=8)


if __name__ == "__main__":
    unittest.main()
