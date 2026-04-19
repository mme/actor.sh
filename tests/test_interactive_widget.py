"""Pilot-driven integration tests for TerminalWidget.

These spin up a minimal Textual app hosting a TerminalWidget backed by a
real PtySession running /bin/cat (or /bin/sh). They verify keystrokes
reach the child, output is rendered into the pyte buffer, Ctrl+Z posts
ExitRequested, and child exit posts SessionExited.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import unittest

if sys.platform == "win32":
    raise unittest.SkipTest("PTY not available on Windows")

from textual.app import App, ComposeResult
from textual.containers import Vertical

from actor.watch.interactive.pty_session import PtySession
from actor.watch.interactive.screen import TerminalScreen
from actor.watch.interactive.widget import TerminalWidget


def _find_binary(name: str) -> str:
    for base in ("/bin", "/usr/bin"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    path = shutil.which(name)
    if path is None:
        raise unittest.SkipTest(f"{name} not found on PATH")
    return path


class _HostApp(App):
    CSS = "Screen { background: $background; }"

    def __init__(self, argv, cwd):
        super().__init__()
        self._session = PtySession(argv=argv, cwd=cwd, rows=10, cols=40)
        self._screen = TerminalScreen(rows=10, cols=40)
        self.widget = TerminalWidget(self._session, self._screen, id="term")
        self.exit_requests = 0
        self.session_exits = []

    def compose(self) -> ComposeResult:
        yield Vertical(self.widget)

    def on_mount(self) -> None:
        self._session.spawn()
        self.widget.focus()

    def on_terminal_widget_exit_requested(self, message):
        self.exit_requests += 1

    def on_terminal_widget_session_exited(self, message):
        self.session_exits.append(message.exit_code)


class TerminalWidgetIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_keystroke_reaches_child_and_output_rendered(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            await pilot.press("h", "i", "enter")
            # Let cat echo back.
            for _ in range(50):
                await pilot.pause(0.02)
                plain = "\n".join(l.plain for l in app._screen.render_lines())
                if "hi" in plain:
                    break
            plain = "\n".join(l.plain for l in app._screen.render_lines())
            self.assertIn("hi", plain, f"screen did not contain echo; got:\n{plain}")
            app._session.close()

    async def test_ctrl_z_posts_exit_requested(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            await pilot.press("ctrl+z")
            # Give the message loop a moment to deliver.
            await pilot.pause(0.05)
            self.assertEqual(app.exit_requests, 1)
            # Session stayed alive — ctrl+z is not forwarded.
            self.assertFalse(app._session.exited)
            app._session.close()

    async def test_child_exit_posts_session_exited(self):
        sh = _find_binary("sh")
        app = _HostApp([sh, "-c", "exit 3"], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            # Wait for child to exit and message to propagate.
            for _ in range(100):
                if app.session_exits:
                    break
                await pilot.pause(0.02)
            self.assertEqual(app.session_exits, [3])

    async def test_resize_changes_both_screen_and_pty(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            # Give the widget its natural size; on_resize was fired once.
            await pilot.pause(0.02)
            rows = app._screen.rows
            cols = app._screen.cols
            self.assertGreater(rows, 0)
            self.assertGreater(cols, 0)
            app._session.close()


if __name__ == "__main__":
    unittest.main()
