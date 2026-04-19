"""Unit tests for the PURE interactive-terminal modules.

Covers screen.py, input.py, batcher.py, diagnostics.py — no subprocess,
no event loop, no Textual harness. If anything here breaks, flicker /
keyboard / mouse behaviour is broken, and we want the signal before
spawning real processes.
"""
from __future__ import annotations

import unittest

from actor.watch.interactive.batcher import RefreshBatcher
from actor.watch.interactive.diagnostics import DiagnosticRecorder, EventKind
from actor.watch.interactive.input import (
    MouseButton,
    MouseMode,
    key_to_bytes,
    mouse_press_to_bytes,
    mouse_release_to_bytes,
)
from actor.watch.interactive.screen import TerminalScreen, _resolve_color


# --- Screen ---------------------------------------------------------------

class TerminalScreenTests(unittest.TestCase):
    def test_plain_text_lands_at_cursor(self):
        s = TerminalScreen(rows=4, cols=10)
        s.feed(b"hello")
        lines = s.render_lines()
        self.assertEqual(lines[0].plain, "hello     ")
        self.assertEqual(s.cursor, (5, 0))

    def test_ansi_color_preserved(self):
        s = TerminalScreen(rows=2, cols=10)
        s.feed(b"\x1b[31mR\x1b[32mG\x1b[0m")
        lines = s.render_lines()
        # Two distinct-style spans in line 0. Iterate spans via .spans.
        spans = [sp for sp in lines[0].spans]
        # At least one span with red and one with green
        styles = [str(sp.style) for sp in spans]
        self.assertTrue(any("red" in st or "color(1)" in st for st in styles),
                        f"no red span in {styles!r}")
        self.assertTrue(any("green" in st or "color(2)" in st for st in styles),
                        f"no green span in {styles!r}")

    def test_newline_advances_cursor(self):
        s = TerminalScreen(rows=4, cols=10)
        s.feed(b"line1\r\nline2")
        lines = s.render_lines()
        self.assertEqual(lines[0].plain.rstrip(), "line1")
        self.assertEqual(lines[1].plain.rstrip(), "line2")

    def test_resize_keeps_cursor_valid(self):
        s = TerminalScreen(rows=4, cols=10)
        s.feed(b"hello")
        s.resize(rows=6, cols=20)
        self.assertEqual(s.rows, 6)
        self.assertEqual(s.cols, 20)
        lines = s.render_lines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(len(lines[0].plain), 20)

    def test_app_cursor_mode_tracked(self):
        s = TerminalScreen()
        self.assertFalse(s.app_cursor)
        s.feed(b"\x1b[?1h")
        self.assertTrue(s.app_cursor)
        s.feed(b"\x1b[?1l")
        self.assertFalse(s.app_cursor)

    def test_mouse_modes_tracked(self):
        s = TerminalScreen()
        self.assertFalse(s.mouse_mode.tracking)
        self.assertFalse(s.mouse_mode.sgr)
        s.feed(b"\x1b[?1000;1006h")
        self.assertTrue(s.mouse_mode.tracking)
        self.assertTrue(s.mouse_mode.sgr)
        s.feed(b"\x1b[?1000l")
        self.assertFalse(s.mouse_mode.tracking)

    def test_resolve_color_handles_pyte_encodings(self):
        # Named pyte aliases
        self.assertEqual(_resolve_color("red"), "color(1)")
        self.assertEqual(_resolve_color("brightgreen"), "color(10)")
        # "default" is terminal default — no color.
        self.assertIsNone(_resolve_color("default"))
        self.assertIsNone(_resolve_color(None))
        # Truecolor hex: digit-only (was the crash case — '999999' must not
        # be treated as a 256-palette index).
        self.assertEqual(_resolve_color("999999"), "#999999")
        self.assertEqual(_resolve_color("ff00aa"), "#ff00aa")
        # With leading '#' too.
        self.assertEqual(_resolve_color("#123456"), "#123456")
        # 256-palette index (1-3 digits, <=255).
        self.assertEqual(_resolve_color("42"), "color(42)")
        self.assertEqual(_resolve_color("255"), "color(255)")
        # Out-of-range palette index falls back to None.
        self.assertIsNone(_resolve_color("256"))
        # Garbage degrades to None, not crash.
        self.assertIsNone(_resolve_color("not-a-color"))

    def test_rendering_tolerates_truecolor_fg(self):
        """Regression: claude uses truecolor — ensure render_lines doesn't crash."""
        s = TerminalScreen(rows=2, cols=10)
        # 24-bit SGR: CSI 38 ; 2 ; R ; G ; B m
        s.feed(b"\x1b[38;2;153;153;153mX\x1b[0m")
        lines = s.render_lines()
        self.assertEqual(lines[0].plain[0], "X")

    def test_cursor_overlay_inverts_single_cell(self):
        s = TerminalScreen(rows=2, cols=5)
        s.feed(b"abc")  # cursor now at (3, 0)
        lines = s.render_lines()
        # The cell at cursor has a "reverse" style applied.
        # Find the span covering cursor_x=3.
        cursor_x = s.cursor[0]
        inverted = False
        for sp in lines[0].spans:
            if sp.start <= cursor_x < sp.end and "reverse" in str(sp.style):
                inverted = True
                break
        self.assertTrue(inverted, "cursor cell should be reverse-styled")


# --- Input (keys) ---------------------------------------------------------

class KeyTranslationTests(unittest.TestCase):
    def test_printable(self):
        self.assertEqual(key_to_bytes("a", "a"), b"a")

    def test_enter_is_cr(self):
        self.assertEqual(key_to_bytes("enter"), b"\r")

    def test_backspace_is_del(self):
        self.assertEqual(key_to_bytes("backspace"), b"\x7f")

    def test_tab_and_shift_tab(self):
        self.assertEqual(key_to_bytes("tab"), b"\t")
        self.assertEqual(key_to_bytes("shift+tab"), b"\x1b[Z")

    def test_arrow_default_is_csi(self):
        self.assertEqual(key_to_bytes("up"), b"\x1b[A")
        self.assertEqual(key_to_bytes("down"), b"\x1b[B")
        self.assertEqual(key_to_bytes("right"), b"\x1b[C")
        self.assertEqual(key_to_bytes("left"), b"\x1b[D")

    def test_arrow_app_cursor_uses_ss3(self):
        self.assertEqual(key_to_bytes("up", app_cursor=True), b"\x1bOA")
        self.assertEqual(key_to_bytes("left", app_cursor=True), b"\x1bOD")

    def test_ctrl_letter(self):
        self.assertEqual(key_to_bytes("ctrl+c"), b"\x03")
        self.assertEqual(key_to_bytes("ctrl+a"), b"\x01")
        self.assertEqual(key_to_bytes("ctrl+z"), b"\x1a")

    def test_alt_letter(self):
        self.assertEqual(key_to_bytes("alt+x"), b"\x1bx")

    def test_function_keys(self):
        self.assertEqual(key_to_bytes("f1"), b"\x1bOP")
        self.assertEqual(key_to_bytes("f12"), b"\x1b[24~")

    def test_unknown_returns_none(self):
        self.assertIsNone(key_to_bytes("weird-key"))

    def test_unicode_character(self):
        self.assertEqual(key_to_bytes("é", "é"), "é".encode("utf-8"))


# --- Input (mouse) --------------------------------------------------------

class MouseEncodingTests(unittest.TestCase):
    def test_no_tracking_no_bytes(self):
        mode = MouseMode()  # all off
        self.assertIsNone(
            mouse_press_to_bytes(MouseButton.LEFT, 5, 3, mode)
        )

    def test_sgr_left_click(self):
        mode = MouseMode(tracking=True, sgr=True)
        # 0-based (5,3) -> 1-based (6,4)
        self.assertEqual(
            mouse_press_to_bytes(MouseButton.LEFT, 5, 3, mode),
            b"\x1b[<0;6;4M",
        )

    def test_sgr_release(self):
        mode = MouseMode(tracking=True, sgr=True)
        self.assertEqual(
            mouse_release_to_bytes(5, 3, mode),
            b"\x1b[<0;6;4m",
        )

    def test_sgr_wheel_up(self):
        mode = MouseMode(tracking=True, sgr=True)
        self.assertEqual(
            mouse_press_to_bytes(MouseButton.WHEEL_UP, 0, 0, mode),
            b"\x1b[<64;1;1M",
        )

    def test_sgr_wheel_down(self):
        mode = MouseMode(tracking=True, sgr=True)
        self.assertEqual(
            mouse_press_to_bytes(MouseButton.WHEEL_DOWN, 0, 0, mode),
            b"\x1b[<65;1;1M",
        )

    def test_legacy_x10_format(self):
        mode = MouseMode(tracking=True, sgr=False)
        got = mouse_press_to_bytes(MouseButton.LEFT, 5, 3, mode)
        # ESC [ M  <cb>  <cx>  <cy>  -- all offset by 32
        self.assertEqual(got[:3], b"\x1b[M")
        self.assertEqual(got[3], 32 + 0)
        self.assertEqual(got[4], 32 + 6)
        self.assertEqual(got[5], 32 + 4)

    def test_legacy_clamps_to_protocol_max(self):
        mode = MouseMode(tracking=True, sgr=False)
        got = mouse_press_to_bytes(MouseButton.LEFT, 500, 500, mode)
        # Legacy can't exceed 223, so should clamp.
        self.assertEqual(got[4], 32 + 223)
        self.assertEqual(got[5], 32 + 223)


# --- Batcher --------------------------------------------------------------

class RefreshBatcherTests(unittest.TestCase):
    def test_no_bytes_no_refresh(self):
        b = RefreshBatcher()
        self.assertFalse(b.should_refresh_now(now=0.0))

    def test_first_chunk_refreshes_immediately(self):
        b = RefreshBatcher(min_interval=0.01, max_defer=0.05)
        b.on_bytes(10, now=0.0)
        self.assertTrue(b.should_refresh_now(now=0.0))

    def test_rapid_chunks_coalesce_under_min_interval(self):
        b = RefreshBatcher(min_interval=0.01, max_defer=0.05)
        b.on_bytes(10, now=0.0)
        self.assertTrue(b.should_refresh_now(now=0.0))
        b.mark_refreshed(now=0.0)
        # New bytes arrive quickly — should NOT refresh.
        b.on_bytes(10, now=0.002)
        self.assertFalse(b.should_refresh_now(now=0.002))
        b.on_bytes(10, now=0.004)
        self.assertFalse(b.should_refresh_now(now=0.004))
        # After min_interval elapses, refresh fires.
        self.assertTrue(b.should_refresh_now(now=0.011))

    def test_max_defer_fires_under_sustained_load(self):
        b = RefreshBatcher(min_interval=0.01, max_defer=0.03)
        b.on_bytes(10, now=0.0)
        b.mark_refreshed(now=0.0)
        b.on_bytes(10, now=0.001)
        # We're still below min_interval...
        self.assertFalse(b.should_refresh_now(now=0.005))
        # ...but max_defer measured since pending_since forces a refresh.
        self.assertTrue(b.should_refresh_now(now=0.031))

    def test_mark_refreshed_clears_pending(self):
        b = RefreshBatcher()
        b.on_bytes(42, now=0.0)
        self.assertEqual(b.pending_bytes(), 42)
        flushed = b.mark_refreshed(now=0.0)
        self.assertEqual(flushed, 42)
        self.assertEqual(b.pending_bytes(), 0)

    def test_coalesce_count(self):
        """10 tiny chunks inside min_interval → exactly 1 refresh."""
        b = RefreshBatcher(min_interval=0.01, max_defer=0.05)
        refresh_count = 0
        for i in range(10):
            t = 0.0 + i * 0.0005   # 0.0, 0.0005, ... all under 0.005
            b.on_bytes(1, now=t)
            if b.should_refresh_now(t):
                b.mark_refreshed(t)
                refresh_count += 1
        # Drain anything remaining at end.
        if b.should_refresh_now(0.02):
            b.mark_refreshed(0.02)
            refresh_count += 1
        self.assertEqual(refresh_count, 2,
            "expected 1 initial + 1 drain refresh, got %d" % refresh_count)


# --- Diagnostics ---------------------------------------------------------

class DiagnosticRecorderTests(unittest.TestCase):
    def test_records_preview(self):
        r = DiagnosticRecorder(capacity=10)
        r.record(EventKind.READ, b"hello world")
        events = r.recent()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.READ)
        self.assertEqual(events[0].size, 11)
        self.assertEqual(events[0].preview, b"hello world")

    def test_preview_truncated(self):
        r = DiagnosticRecorder()
        long = b"x" * 200
        r.record(EventKind.WRITE, long)
        ev = r.recent()[0]
        self.assertEqual(ev.size, 200)
        self.assertEqual(len(ev.preview), 32)

    def test_ring_buffer_overwrites(self):
        r = DiagnosticRecorder(capacity=3)
        for i in range(5):
            r.record(EventKind.READ, str(i).encode())
        events = r.recent()
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].preview, b"2")
        self.assertEqual(events[-1].preview, b"4")

    def test_recent_limit(self):
        r = DiagnosticRecorder(capacity=10)
        for i in range(5):
            r.record(EventKind.READ, str(i).encode())
        last2 = r.recent(limit=2)
        self.assertEqual(len(last2), 2)
        self.assertEqual(last2[0].preview, b"3")
        self.assertEqual(last2[1].preview, b"4")

    def test_clear(self):
        r = DiagnosticRecorder()
        r.record(EventKind.READ, b"x")
        r.clear()
        self.assertEqual(len(r), 0)

    def test_format_produces_readable_text(self):
        r = DiagnosticRecorder(now=lambda: 12.345)
        r.record(EventKind.READ, b"hi", note="test")
        out = r.format()
        self.assertIn("read", out)
        self.assertIn("size=    2", out)
        self.assertIn("test", out)

    def test_injectable_clock(self):
        t = [0.0]
        r = DiagnosticRecorder(now=lambda: t[0])
        r.record(EventKind.READ, b"a")
        t[0] = 5.0
        r.record(EventKind.WRITE, b"b")
        events = r.recent()
        self.assertEqual(events[0].t, 0.0)
        self.assertEqual(events[1].t, 5.0)


if __name__ == "__main__":
    unittest.main()
