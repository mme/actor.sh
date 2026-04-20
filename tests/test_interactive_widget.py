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

from actor.watch.interactive.input import MouseMode
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
            # Cat echoes input; wait for two lines ("hi" on the typed row,
            # "hi" echoed on the next) or up to 1s.
            for _ in range(50):
                await pilot.pause(0.02)
                lines = [l.plain.rstrip() for l in app._screen.render_lines()]
                if lines[0] == "hi" and lines[1] == "hi":
                    break
            lines = [l.plain.rstrip() for l in app._screen.render_lines()]
            self.assertEqual(
                lines[0], "hi",
                f"first row should contain the typed input; got {lines!r}",
            )
            self.assertEqual(
                lines[1], "hi",
                f"second row should contain cat's echo; got {lines!r}",
            )
            app._session.close()

    async def test_ctrl_z_posts_exit_requested(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            await pilot.press("ctrl+z")
            # Poll rather than rely on a fixed timing budget.
            for _ in range(50):
                if app.exit_requests >= 1:
                    break
                await pilot.pause(0.01)
            self.assertEqual(app.exit_requests, 1)
            self.assertFalse(app._session.exited,
                             "ctrl+z must not forward to the PTY")
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
        import fcntl, struct, termios
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(60, 20)) as pilot:
            # First on_resize has fired; widget should have adopted the
            # available cell size from its mounted container.
            await pilot.pause(0.02)
            first_rows = app._screen.rows
            first_cols = app._screen.cols

            # Pilot can't cleanly shrink a sub-widget; call the same methods
            # on_resize would call, then verify the PTY's winsize updated.
            app.widget._screen.resize(rows=12, cols=30)
            app.widget._session.resize(rows=12, cols=30)
            await pilot.pause(0.01)
            packed = fcntl.ioctl(
                app._session.fd, termios.TIOCGWINSZ, b"\0" * 8,
            )
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            self.assertEqual((rows, cols), (12, 30),
                             f"PTY winsize not updated; initial was ({first_rows}, {first_cols})")
            self.assertEqual(app.widget._screen.rows, 12)
            self.assertEqual(app.widget._screen.cols, 30)
            app._session.close()


class InitialRenderTests(unittest.IsolatedAsyncioTestCase):
    """Before the child has produced any output, the widget should
    render as blank — otherwise the empty pyte cursor + unused column
    fill briefly flashes as a dark box while Textual's first layout
    settles."""

    async def test_placeholder_before_first_output(self):
        """Before the child produces its first frame, non-centered rows
        render blank and the middle row shows a "Connecting…" hint so a
        hung startup isn't an inscrutable blank box."""
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            app.widget._first_output_received = False
            top = app.widget.render_line(0)
            self.assertTrue(
                top.text.strip() == "",
                f"non-center rows should be blank; got {top.text!r}",
            )
            middle = app.widget.render_line(app.widget.size.height // 2)
            self.assertIn(
                "Connecting", middle.text,
                f"center row should show the placeholder; got {middle.text!r}",
            )
            # After feeding a byte, subsequent renders use the pyte frame.
            app.widget._on_pty_output(b"X")
            self.assertTrue(app.widget._first_output_received)
            app._session.close()


class LocalScrollTests(unittest.IsolatedAsyncioTestCase):
    """PageUp/PageDown/wheel scroll the pyte history locally when the
    child isn't in alt-screen and hasn't enabled mouse tracking."""

    async def test_pageup_scrolls_history_when_no_alt_screen(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            written: list[bytes] = []
            orig = app.widget._session.write
            app.widget._session.write = lambda data, _orig=orig, w=written: (
                w.append(data), _orig(data),
            )[1]
            # Generate enough lines to fill the scrollback.
            for i in range(30):
                app.widget._screen.feed(f"line{i}\r\n".encode())
            await pilot.press("pageup")
            await pilot.pause(0.02)
            self.assertEqual(
                written, [],
                f"pageup should scroll locally, not forward bytes; got {written!r}",
            )
            app._session.close()

    async def test_pageup_forwarded_when_alt_screen(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            # Simulate child entering alt-screen.
            app.widget._screen.feed(b"\x1b[?1049h")
            written: list[bytes] = []
            orig = app.widget._session.write
            app.widget._session.write = lambda data, _orig=orig, w=written: (
                w.append(data), _orig(data),
            )[1]
            await pilot.press("pageup")
            await pilot.pause(0.02)
            self.assertEqual(
                written, [b"\x1b[5~"],
                f"alt-screen pageup must forward to child; got {written!r}",
            )
            app._session.close()

    async def test_scroll_wheel_scrolls_history_not_child(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            for i in range(30):
                app.widget._screen.feed(f"row{i}\r\n".encode())
            written: list[bytes] = []
            orig = app.widget._session.write
            app.widget._session.write = lambda data, _orig=orig, w=written: (
                w.append(data), _orig(data),
            )[1]
            # Build + dispatch a real MouseScrollUp through the handler.
            from unittest.mock import MagicMock
            fake = MagicMock(spec_set=["x", "y", "stop"])
            fake.x, fake.y = 1, 1
            await app.widget.on_mouse_scroll_up(fake)
            fake.stop.assert_called_once()
            self.assertEqual(
                written, [],
                "scroll-wheel-up in local mode must not forward bytes to child",
            )
            app._session.close()


class RenderCacheTests(unittest.IsolatedAsyncioTestCase):
    """The widget's render_line cache should build the strip list once
    per _frame_counter and reuse it for every visible row."""

    async def test_cache_hits_across_rows_in_same_frame(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            calls = {"n": 0}
            real = app.widget._screen.render_lines

            def counting_render_lines():
                calls["n"] += 1
                return real()

            app.widget._screen.render_lines = counting_render_lines  # type: ignore
            # Invalidate any cache Textual populated during pilot setup.
            app.widget._cached_strips = None
            app.widget._cached_at_frame = -1
            # Skip the pre-first-output blank-render guard for the cache
            # test: we want to exercise the strip-builder path.
            app.widget._first_output_received = True
            calls["n"] = 0
            for y in range(app.widget._screen.rows):
                app.widget.render_line(y)
            self.assertEqual(
                calls["n"], 1,
                "render_line should build once per frame and cache; "
                f"got {calls['n']} calls for {app.widget._screen.rows} rows",
            )
            app._session.close()

    async def test_cache_invalidates_on_frame_bump(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            calls = {"n": 0}
            real = app.widget._screen.render_lines

            def counting_render_lines():
                calls["n"] += 1
                return real()

            app.widget._screen.render_lines = counting_render_lines  # type: ignore
            app.widget._cached_strips = None
            app.widget._cached_at_frame = -1
            app.widget._first_output_received = True
            calls["n"] = 0
            app.widget.render_line(0)
            app.widget._frame_counter += 1
            app.widget.render_line(0)
            self.assertEqual(
                calls["n"], 2,
                f"frame bump should invalidate the cache; got {calls['n']} calls",
            )
            app._session.close()


class MouseInputTests(unittest.IsolatedAsyncioTestCase):
    """Verify Textual mouse events reach the PTY as SGR-encoded bytes, and
    that we don't double-send on click (MouseDown + Click in Textual)."""

    async def test_mouse_down_writes_once(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            # Enable SGR click tracking as if the child had sent DECSET 1000/1006.
            app._screen.feed(b"\x1b[?1000;1006h")
            written: list[bytes] = []
            orig = app.widget._session.write
            app.widget._session.write = lambda data, _orig=orig, w=written: (
                w.append(data), _orig(data),
            )[1]
            await pilot.click("#term", offset=(5, 3))
            await pilot.pause(0.05)
            press = [b for b in written if b.startswith(b"\x1b[<0;") and b.endswith(b"M")]
            release = [b for b in written if b.startswith(b"\x1b[<") and b.endswith(b"m")]
            # Exactly one press per click; no doubles.
            self.assertEqual(
                len(press), 1,
                f"expected one press sequence, got {len(press)}: {written!r}",
            )
            self.assertEqual(
                len(release), 1,
                f"expected one release sequence, got {len(release)}: {written!r}",
            )
            app._session.close()

    async def test_scroll_wheel_emits_wheel_sequence(self):
        cat = _find_binary("cat")
        app = _HostApp([cat], pathlib.Path("/tmp"))
        async with app.run_test(size=(40, 10)) as pilot:
            app._screen.feed(b"\x1b[?1000;1006h")
            written: list[bytes] = []
            orig = app.widget._session.write
            app.widget._session.write = lambda data, _orig=orig, w=written: (
                w.append(data), _orig(data),
            )[1]
            # Invoke the handler directly rather than forging a Textual event —
            # the event signature varies across versions, but the handler is
            # the part we own.
            from actor.watch.interactive.input import MouseButton
            app.widget._emit_mouse(MouseButton.WHEEL_UP, x=2, y=3)
            await pilot.pause(0.02)
            self.assertEqual(
                written, [b"\x1b[<64;3;4M"],
                f"expected SGR wheel-up sequence at (x+1=3, y+1=4); got {written!r}",
            )
            app._session.close()


if __name__ == "__main__":
    unittest.main()
