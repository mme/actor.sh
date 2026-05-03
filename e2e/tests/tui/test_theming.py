"""e2e TUI: theme registration."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import watch_app


class ThemingTests(unittest.IsolatedAsyncioTestCase):

    async def test_default_theme_is_claude_dark(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                # on_ready registers and applies claude-dark.
                self.assertEqual(app.theme, "claude-dark")

    async def test_claude_light_theme_is_registered(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                # If the theme is registered, switching to it is a no-op.
                app.theme = "claude-light"
                await pilot.pause(0.05)
                self.assertEqual(app.theme, "claude-light")


if __name__ == "__main__":
    unittest.main()
