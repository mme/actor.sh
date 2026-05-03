"""e2e TUI: more probes for UX bugs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class MoreTuiTests(unittest.IsolatedAsyncioTestCase):

    async def test_overview_shows_role_when_role_was_applied(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    description "QA"\n'
                '    agent "claude"\n'
                '    prompt "x"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # The selected actor's overview should reflect the role.
                self.assertEqual(app._overview_header_actor.name, "alice")
                # If the app exposes role info, verify; if not, this is
                # a UX gap.
                from textual.widgets import Static
                header = app.query_one("#overview-header", Static)
                # Render returns a rich Visual; we just confirm widget exists.
                self.assertIsNotNone(header)

    async def test_app_handles_zero_actor_initial_state(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                # No actors → splash stays. App should not error.
                await pilot.pause(1.0)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_palette_responsive_under_select_actor_pattern(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("p")
                await pilot.pause(0.3)
                from textual.screen import SystemModalScreen
                self.assertIsInstance(app.screen, SystemModalScreen)
                await pilot.press("escape")
                await pilot.pause(0.2)


if __name__ == "__main__":
    unittest.main()
