"""Textual widget glueing PtySession + TerminalScreen + input translation."""
from __future__ import annotations

import time
from typing import Optional

from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.timer import Timer
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
        def __init__(self, widget: "TerminalWidget") -> None:
            self.widget = widget
            super().__init__()

    class SessionExited(Message):
        def __init__(self, widget: "TerminalWidget", exit_code: int) -> None:
            self.widget = widget
            self.exit_code = exit_code
            super().__init__()

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
        self._cached_strips: Optional[list[Strip]] = None
        self._cached_at_frame: int = -1
        # Single pending timer for deferred refresh — stacking N timers on
        # bursty output would waste memory and re-render cycles.
        self._deferred_timer: Optional[Timer] = None
        # Callbacks set once at construction; surviving unmount/remount
        # (the manager owns lifetime, not the DOM). Both callbacks are
        # set via `set_callbacks` so consumers that need additional hooks
        # (manager's DB finalize) can chain without clobbering the widget's.
        session.set_callbacks(on_output=self._on_pty_output, on_exit=self._on_pty_exit)

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
            self._schedule_deferred_refresh()

    def _schedule_deferred_refresh(self) -> None:
        if self._deferred_timer is not None:
            return
        self._deferred_timer = self.set_timer(
            self._batcher.max_defer, self._check_deferred_refresh,
        )

    def _check_deferred_refresh(self) -> None:
        self._deferred_timer = None
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
        strips = self._get_strips()
        if 0 <= y < len(strips):
            return strips[y]
        return Strip.blank(self.size.width)

    def _get_strips(self) -> list[Strip]:
        if self._cached_at_frame == self._frame_counter and self._cached_strips is not None:
            return self._cached_strips
        console = self.app.console
        self._cached_strips = [
            Strip(list(line.render(console)))
            for line in self._screen.render_lines()
        ]
        self._cached_at_frame = self._frame_counter
        return self._cached_strips

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
    # Textual fires MouseDown -> MouseUp -> Click. on_click is intentionally
    # NOT handled here: it would double-send press bytes since on_mouse_down
    # already emits them.

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
        self._emit_mouse(MouseButton.WHEEL_UP, event.x, event.y, event)

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._emit_mouse(MouseButton.WHEEL_DOWN, event.x, event.y, event)

    def _handle_mouse_press(self, x: int, y: int, button: int) -> None:
        btn_map = {1: MouseButton.LEFT, 2: MouseButton.MIDDLE, 3: MouseButton.RIGHT}
        mbtn = btn_map.get(button)
        if mbtn is None:
            return
        self._emit_mouse(mbtn, x, y)

    def _emit_mouse(self, button: MouseButton, x: int, y: int, event: Optional[events.Event] = None) -> None:
        data = mouse_press_to_bytes(button, x, y, self._screen.mouse_mode)
        if data is None:
            return
        if event is not None:
            event.stop()
        if self._recorder is not None:
            self._recorder.record(EventKind.WRITE, data)
        self._session.write(data)
