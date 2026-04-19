"""Textual widget glueing PtySession + TerminalScreen + input translation.

The widget holds the live pyte screen, receives output from the session,
and translates user key/mouse events into bytes written to the session.
Rendering is coalesced through RefreshBatcher so bursty output doesn't
flicker.

Ctrl+Z is intercepted by the widget (not forwarded to the PTY) and emits
an `ExitInteractive` message so the host app can swap back to the log
view. Every other key / mouse event routes through to the child.
"""
from __future__ import annotations

import time
from typing import List, Optional

from rich.console import ConsoleOptions, RenderResult
from rich.text import Text
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget

from .batcher import RefreshBatcher
from .diagnostics import DiagnosticRecorder, EventKind
from .input import (
    MouseButton,
    key_to_bytes,
    mouse_press_to_bytes,
    mouse_release_to_bytes,
)
from .pty_session import PtySession
from .screen import TerminalScreen


class TerminalWidget(Widget, can_focus=True):
    """An embedded terminal view bound to a PtySession."""

    DEFAULT_CSS = """
    TerminalWidget {
        background: $background;
        color: $foreground;
    }
    """

    class ExitRequested(Message):
        """User hit Ctrl+Z to leave interactive mode."""

        def __init__(self, widget: "TerminalWidget") -> None:
            self.widget = widget
            super().__init__()

    class SessionExited(Message):
        """The PtySession's child process exited."""

        def __init__(self, widget: "TerminalWidget", exit_code: int) -> None:
            self.widget = widget
            self.exit_code = exit_code
            super().__init__()

    # Expose pending output as a reactive so Textual re-renders on change.
    _frame_counter: reactive[int] = reactive(0)

    def __init__(
        self,
        session: PtySession,
        screen: TerminalScreen,
        *,
        recorder: Optional[DiagnosticRecorder] = None,
        name: Optional[str] = None,
        id: Optional[str] = None,
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._session = session
        self._screen = screen
        self._batcher = RefreshBatcher()
        self._recorder = recorder
        self._exit_code: Optional[int] = None
        # Wire the session callbacks once at construction time so that
        # unmounting and re-mounting (e.g. when the user switches actors
        # and comes back) doesn't lose output or miss the exit event.
        # The screen is updated unconditionally; render refresh only
        # happens while we're mounted (post_message is a no-op otherwise).
        session._on_output = self._on_pty_output  # type: ignore[attr-defined]
        session._on_exit = self._on_pty_exit      # type: ignore[attr-defined]

    # -- session callbacks -------------------------------------------------

    def _on_pty_output(self, data: bytes) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.READ, data)
        self._screen.feed(data)
        now = time.monotonic()
        self._batcher.on_bytes(len(data), now)
        if self._batcher.should_refresh_now(now):
            self._flush_refresh(now)
        else:
            # Schedule a delayed check so max_defer still fires if bytes stop.
            self.set_timer(self._batcher.max_defer, self._check_deferred_refresh)

    def _check_deferred_refresh(self) -> None:
        now = time.monotonic()
        if self._batcher.should_refresh_now(now):
            self._flush_refresh(now)

    def _flush_refresh(self, now: float) -> None:
        self._batcher.mark_refreshed(now)
        if self._recorder is not None:
            self._recorder.record(EventKind.REFRESH)
        self._frame_counter += 1

    def _on_pty_exit(self, exit_code: int) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.EXIT, note=f"code={exit_code}")
        self._exit_code = exit_code
        self.post_message(self.SessionExited(self, exit_code))

    # -- rendering ---------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        lines = self._cached_lines()
        if 0 <= y < len(lines):
            return Strip(lines[y].render(self.app.console))
        return Strip.blank(self.size.width)

    def _cached_lines(self) -> List[Text]:
        return self._screen.render_lines()

    # -- resize ------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        rows = max(1, event.size.height)
        cols = max(1, event.size.width)
        if rows == self._screen.rows and cols == self._screen.cols:
            return
        self._screen.resize(rows=rows, cols=cols)
        self._session.resize(rows=rows, cols=cols)
        if self._recorder is not None:
            self._recorder.record(EventKind.RESIZE, note=f"{rows}x{cols}")

    # -- key input ---------------------------------------------------------

    async def on_key(self, event: events.Key) -> None:
        # Intercept Ctrl+Z — that's our "leave interactive mode" shortcut.
        if event.key == "ctrl+z":
            event.stop()
            event.prevent_default()
            self.post_message(self.ExitRequested(self))
            return

        data = key_to_bytes(
            event.key,
            event.character,
            app_cursor=self._screen.app_cursor,
        )
        if data is None:
            return
        event.stop()
        event.prevent_default()
        if self._recorder is not None:
            self._recorder.record(EventKind.WRITE, data)
        self._session.write(data)

    # -- mouse input -------------------------------------------------------

    async def on_click(self, event: events.Click) -> None:
        self._handle_mouse_press(event.x, event.y, button=event.button)

    async def on_mouse_down(self, event: events.MouseDown) -> None:
        self._handle_mouse_press(event.x, event.y, button=event.button)

    async def on_mouse_up(self, event: events.MouseUp) -> None:
        data = mouse_release_to_bytes(event.x, event.y, self._screen.mouse_mode)
        if data is None:
            return
        event.stop()
        if self._recorder is not None:
            self._recorder.record(EventKind.WRITE, data)
        self._session.write(data)

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        data = mouse_press_to_bytes(
            MouseButton.WHEEL_UP, event.x, event.y, self._screen.mouse_mode,
        )
        if data is None:
            return
        event.stop()
        if self._recorder is not None:
            self._recorder.record(EventKind.WRITE, data)
        self._session.write(data)

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        data = mouse_press_to_bytes(
            MouseButton.WHEEL_DOWN, event.x, event.y, self._screen.mouse_mode,
        )
        if data is None:
            return
        event.stop()
        if self._recorder is not None:
            self._recorder.record(EventKind.WRITE, data)
        self._session.write(data)

    def _handle_mouse_press(self, x: int, y: int, button: int) -> None:
        # Textual button: 1=left, 2=middle, 3=right.
        btn_map = {1: MouseButton.LEFT, 2: MouseButton.MIDDLE, 3: MouseButton.RIGHT}
        mbtn = btn_map.get(button)
        if mbtn is None:
            return
        data = mouse_press_to_bytes(mbtn, x, y, self._screen.mouse_mode)
        if data is None:
            return
        if self._recorder is not None:
            self._recorder.record(EventKind.WRITE, data)
        self._session.write(data)
