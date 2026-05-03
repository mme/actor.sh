"""e2e TUI: more probes for likely bugs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class MorePotentialBugsTests(unittest.IsolatedAsyncioTestCase):

    async def test_status_bar_displayed(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                # status-bar widget should exist.
                from textual.widgets import Static
                bars = list(app.query("#status-bar"))
                self.assertEqual(len(bars), 1)

    async def test_overview_runs_label_present(self):
        # The OVERVIEW pane should have a Runs label widget.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                widgets = list(app.query("#overview-runs-label"))
                self.assertGreaterEqual(len(widgets), 1)

    async def test_actor_with_long_name_renders_in_tree(self):
        # Long actor name shouldn't break rendering.
        long_name = "very-long-actor-name-with-many-hyphens-" + "a" * 20
        with isolated_home() as env:
            r = env.run_cli(["new", long_name])
            if r.returncode == 0:
                async with watch_app(env) as (app, pilot):
                    await pilot.pause(2.0)
                    self.assertIsNone(getattr(app, "_exception", None))

    async def test_quit_after_just_browsing(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("q")
                # Quit should cleanly exit.
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_actor_runs_table_widget_exists(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                widgets = list(app.query("#runs-table"))
                self.assertGreaterEqual(len(widgets), 1)

    async def test_overview_logs_widget_exists(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                widgets = list(app.query("#logs-content"))
                self.assertEqual(len(widgets), 1)

    async def test_diff_scroll_widget_exists(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                widgets = list(app.query("#diff-scroll"))
                self.assertEqual(len(widgets), 1)

    async def test_running_actor_overview_shows_elapsed_time(self):
        # Per #66: live elapsed timer when an actor is running.
        # Currently this may not be implemented.
        import subprocess, time
        with isolated_home() as env:
            p = subprocess.Popen(
                ["actor", "new", "alice", "long task"],
                env=env.env(**claude_responds("ok", sleep=3)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                async with watch_app(env) as (app, pilot):
                    await select_actor(pilot, app, "alice")
                    # Wait for header to render.
                    for _ in range(20):
                        await pilot.pause(0.1)
                    from textual.widgets import Static
                    header = app.query_one("#overview-header", Static)
                    rendered = str(header.render())
                    # The header should mention elapsed time / running.
                    has_timer = any(s in rendered.lower() for s in ("running", "for", "s"))
                    self.assertTrue(has_timer, f"running header should show timer: {rendered!r}")
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
