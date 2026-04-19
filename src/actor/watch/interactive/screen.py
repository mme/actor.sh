"""Pyte-backed terminal screen with rich.Text rendering + mouse-mode sniffing.

Pure. Accepts bytes via `feed`, exposes the current frame via `render`.
Also sniffs DECSET sequences (mouse tracking, app cursor) as they stream
past so the input layer can adjust its encoding.
"""
from __future__ import annotations

import re
from typing import List, Tuple

import pyte
from pyte.screens import Char

from rich.style import Style
from rich.text import Text

from .input import MouseMode


# Capture `CSI ? <params> h|l` — DECSET/DECRST. We only need the param list
# and whether it's set (h) or reset (l).
_DECSET_RE = re.compile(rb"\x1b\[\?([\d;]+)([hl])")


class TerminalScreen:
    """Wraps pyte.HistoryScreen + rich rendering.

    Rows/cols are cell counts. `scrollback` is the number of lines pyte
    keeps in its off-screen history (each direction).
    """

    def __init__(self, rows: int = 24, cols: int = 80, scrollback: int = 10_000) -> None:
        self._rows = rows
        self._cols = cols
        # HistoryScreen splits scrollback into top/bottom halves internally.
        # Giving each half `scrollback` lines keeps the math simple.
        self._screen = pyte.HistoryScreen(
            cols, rows,
            history=scrollback, ratio=0.5,
        )
        self._stream = pyte.ByteStream(self._screen)
        self.mouse_mode = MouseMode()
        self.app_cursor = False

    # -- Stream in ---------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Feed raw PTY output bytes into the emulator."""
        # Sniff mouse / cursor-mode toggles. We only look at DECSET/DECRST
        # sequences; everything else flows through pyte untouched.
        for m in _DECSET_RE.finditer(data):
            params = [int(p) for p in m.group(1).split(b";") if p]
            on = m.group(2) == b"h"
            for p in params:
                self._apply_decset(p, on)
        self._stream.feed(data)

    def _apply_decset(self, param: int, on: bool) -> None:
        if param == 1:          # DECCKM — application cursor keys
            self.app_cursor = on
        elif param == 1000:     # X10 / VT200 button-event tracking
            self.mouse_mode.tracking = on
        elif param == 1002:     # button-event tracking with drag
            self.mouse_mode.drag = on
        elif param == 1003:     # any-event tracking
            self.mouse_mode.any_motion = on
        elif param == 1006:     # SGR extended mouse coordinates
            self.mouse_mode.sgr = on

    # -- Geometry ----------------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        if rows == self._rows and cols == self._cols:
            return
        self._rows = rows
        self._cols = cols
        self._screen.resize(rows, cols)

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def cursor(self) -> Tuple[int, int]:
        return (self._screen.cursor.x, self._screen.cursor.y)

    # -- Rendering ---------------------------------------------------------

    def render_lines(self) -> List[Text]:
        """Render the current screen buffer as a list of rich.Text lines.

        Scrollback is not included — that's the visible frame only. (The
        scrollback API on pyte.HistoryScreen uses prev_page/next_page; we
        can expose a scrolled offset later if needed.)
        """
        lines: List[Text] = []
        screen = self._screen
        cursor_x, cursor_y = self._screen.cursor.x, self._screen.cursor.y
        cursor_hidden = screen.cursor.hidden

        for y in range(self._rows):
            line = Text()
            buffer_line = screen.buffer[y]
            # pyte stores a sparse dict by x; iterate columns densely.
            x = 0
            while x < self._cols:
                char: Char = buffer_line[x]
                # Coalesce runs of identical-style chars into one segment.
                run_start = x
                run_style = _char_style(char)
                while x < self._cols:
                    nxt = buffer_line[x]
                    if _char_style(nxt) != run_style:
                        break
                    x += 1
                text = "".join(buffer_line[i].data for i in range(run_start, x))
                line.append(text, style=run_style)
            # Cursor overlay: invert one cell unless the child hid the cursor.
            if not cursor_hidden and y == cursor_y and 0 <= cursor_x < self._cols:
                line.stylize("reverse", cursor_x, cursor_x + 1)
            lines.append(line)
        return lines


_PYTE_COLOR_ALIASES = {
    "default": None,
    "black": "color(0)",
    "red": "color(1)",
    "green": "color(2)",
    "brown": "color(3)",
    "blue": "color(4)",
    "magenta": "color(5)",
    "cyan": "color(6)",
    "white": "color(7)",
    "brightblack": "color(8)",
    "brightred": "color(9)",
    "brightgreen": "color(10)",
    "brightbrown": "color(11)",
    "brightblue": "color(12)",
    "brightmagenta": "color(13)",
    "brightcyan": "color(14)",
    "brightwhite": "color(15)",
}


_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _resolve_color(color: str) -> str | None:
    """Map a pyte color string to a rich-compatible color spec.

    Pyte emits:
      - 'default'           — terminal default (return None → rich skips)
      - named colors        — 'red', 'brightblue', etc.
      - 6-char hex          — 'RRGGBB' or '#RRGGBB' (truecolor SGR)
      - 1-3 digit numeric   — '42' (256-palette SGR)
    """
    if color is None or color == "default":
        return None
    if color in _PYTE_COLOR_ALIASES:
        return _PYTE_COLOR_ALIASES[color]
    # Truecolor: hex string with or without the leading '#'.
    if _HEX_RE.match(color):
        return color if color.startswith("#") else f"#{color}"
    # 256-palette index. isdigit() also matches hex digit strings like
    # '999999' — the hex check above must run first.
    if color.isdigit() and 0 <= int(color) <= 255:
        return f"color({color})"
    return None


def _char_style(char: Char) -> Style:
    fg = _resolve_color(char.fg)
    bg = _resolve_color(char.bg)
    if char.reverse:
        fg, bg = bg, fg
    try:
        return Style(
            color=fg,
            bgcolor=bg,
            bold=char.bold,
            italic=char.italics,
            underline=char.underscore,
            strike=char.strikethrough,
            blink=char.blink,
        )
    except Exception:
        # Any unexpected color string (new pyte encoding, malformed sequence)
        # should degrade to plain text rather than crash the render loop.
        return Style(
            bold=char.bold,
            italic=char.italics,
            underline=char.underscore,
            strike=char.strikethrough,
            blink=char.blink,
        )
