"""e2e TUI: OVERVIEW logs RichLog content checks."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class LogsPaneContentTests(unittest.IsolatedAsyncioTestCase):

    async def test_logs_eventually_show_response(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("FOUND_ME_IN_LOGS"))
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                from textual.widgets import RichLog
                log = app.query_one("#logs-content", RichLog)
                # Poll for content to land.
                for _ in range(50):
                    await pilot.pause(0.1)
                    rendered = "\n".join(
                        str(l) for l in getattr(log, "lines", [])
                    )
                    if "FOUND_ME_IN_LOGS" in rendered:
                        break
                rendered = "\n".join(
                    str(l) for l in getattr(log, "lines", [])
                )
                self.assertIn("FOUND_ME_IN_LOGS", rendered,
                              "log content should be visible after polling")

    async def test_logs_for_idle_actor_renders_empty_or_placeholder(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # No runs yet — logs widget shouldn't crash; either empty
                # or a friendly placeholder.
                self.assertIsNone(getattr(app, "_exception", None))


if __name__ == "__main__":
    unittest.main()
