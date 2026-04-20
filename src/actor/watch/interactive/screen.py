"""Pyte-backed terminal screen with rich.Text rendering."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import List, Tuple

import pyte
from pyte.screens import Char

from rich.style import Style
from rich.text import Text

from .input import MouseMode


_DECSET_RE = re.compile(rb"\x1b\[\?([\d;]+)([hl])")

# xterm/DEC private CSI variants (leading <, =, or > after '['). Pyte's
# SGR parser drops the leading symbol and treats e.g. `\x1b[>4;2m`
# (modifyOtherKeys) as SGR 4;2 — underlining every subsequent cell. Strip
# these before feeding pyte. Seen in claude-code init: [>4;2m, [>1u, [>c.
_PRIVATE_CSI_RE = re.compile(rb"\x1b\[[<=>][\d;]*[a-zA-Z~]")


class TerminalScreen:
    def __init__(self, rows: int = 24, cols: int = 80, scrollback: int = 10_000) -> None:
        self._rows = rows
        self._cols = cols
        # HistoryScreen splits history top/bottom via ratio; 0.5 keeps the
        # math symmetric.
        self._screen = pyte.HistoryScreen(
            cols, rows,
            history=scrollback, ratio=0.5,
        )
        self._stream = pyte.ByteStream(self._screen)
        self.mouse_mode = MouseMode()
        self.app_cursor = False
        # DECSET 1049 (or 47 / 1047) swaps to an alternate screen buffer.
        # When active, scrollback is meaningless — the child owns the view
        # and page-up/down shouldn't scroll local history.
        self.alt_screen = False

    def feed(self, data: bytes) -> None:
        for m in _DECSET_RE.finditer(data):
            params = [int(p) for p in m.group(1).split(b";") if p]
            on = m.group(2) == b"h"
            for p in params:
                self._apply_decset(p, on)
        data = _PRIVATE_CSI_RE.sub(b"", data)
        self._stream.feed(data)

    def _apply_decset(self, param: int, on: bool) -> None:
        if param == 1:          # DECCKM — application cursor keys
            self.app_cursor = on
            return
        if param in (47, 1047, 1049):  # alt-screen variants
            self.alt_screen = on
            return
        # MouseMode is frozen; replace wholesale rather than mutate.
        mode = self.mouse_mode
        if param == 1000:       # X10 / VT200 button-event tracking
            self.mouse_mode = replace(mode, tracking=on)
        elif param == 1002:     # button-event tracking with drag
            self.mouse_mode = replace(mode, drag=on)
        elif param == 1003:     # any-event tracking
            self.mouse_mode = replace(mode, any_motion=on)
        elif param == 1006:     # SGR extended mouse coordinates
            self.mouse_mode = replace(mode, sgr=on)

    def history_up(self, lines: int = 1) -> bool:
        """Scroll the visible frame up by `lines` history rows. Returns
        True if anything actually moved. Pyte's HistoryScreen uses pages;
        we call prev_page() once per full page worth of lines."""
        moved = False
        pages = max(1, lines // max(1, self._rows))
        for _ in range(pages):
            before = len(self._screen.history.top)
            self._screen.prev_page()
            if len(self._screen.history.top) != before:
                moved = True
        return moved

    def history_down(self, lines: int = 1) -> bool:
        moved = False
        pages = max(1, lines // max(1, self._rows))
        for _ in range(pages):
            before = len(self._screen.history.bottom)
            self._screen.next_page()
            if len(self._screen.history.bottom) != before:
                moved = True
        return moved

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

    def render_lines(self) -> List[Text]:
        """Render the visible frame as rich.Text (no scrollback)."""
        return self._render_rows(self._screen.buffer, cursor=True)

    def history_size(self) -> int:
        """Lines of scrollback above the visible frame."""
        return len(self._screen.history.top)

    def render_all_lines(self) -> List[Text]:
        """Full virtual content: scrollback-top rows followed by the
        visible frame. Used by the Textual-native scroll path so
        virtual_size can be set correctly and render_line indexes
        across the combined range."""
        lines: List[Text] = list(self._render_history(self._screen.history.top))
        lines.extend(self._render_rows(self._screen.buffer, cursor=True))
        return lines

    def _render_rows(self, rows, cursor: bool) -> List[Text]:
        lines: List[Text] = []
        screen = self._screen
        cursor_x, cursor_y = screen.cursor.x, screen.cursor.y
        cursor_hidden = screen.cursor.hidden
        # Cmdlane-style: respect DECTCEM (\x1b[?25l). Additionally,
        # don't overlay our own reverse on a cell that already has
        # reverse set — the child is painting its own cursor and
        # stacking reverses would cancel it out (invisible cursor).
        cursor_cell_reverse = False
        if not cursor_hidden and 0 <= cursor_y < self._rows and 0 <= cursor_x < self._cols:
            try:
                cursor_cell_reverse = bool(rows[cursor_y][cursor_x].reverse)
            except (KeyError, IndexError, AttributeError):
                cursor_cell_reverse = False
        for y in range(self._rows):
            line = Text()
            try:
                buffer_line = rows[y]
            except (KeyError, IndexError):
                lines.append(line)
                continue
            x = 0
            while x < self._cols:
                char: Char = buffer_line[x]
                run_start = x
                run_style = _char_style(char)
                while x < self._cols:
                    if _char_style(buffer_line[x]) != run_style:
                        break
                    x += 1
                text = "".join(buffer_line[i].data for i in range(run_start, x))
                line.append(text, style=run_style)
            if (
                cursor
                and not cursor_hidden
                and not cursor_cell_reverse
                and y == cursor_y
                and 0 <= cursor_x < self._cols
            ):
                line.stylize("reverse", cursor_x, cursor_x + 1)
            lines.append(line)
        return lines

    def _render_history(self, history) -> List[Text]:
        """Render pyte history rows (list of dicts) as rich.Text. These
        rows never show a cursor since they're already scrolled off."""
        lines: List[Text] = []
        for buffer_line in history:
            line = Text()
            x = 0
            while x < self._cols:
                char = buffer_line.get(x)
                if char is None:
                    line.append(" ")
                    x += 1
                    continue
                run_start = x
                run_style = _char_style(char)
                while x < self._cols:
                    nxt = buffer_line.get(x)
                    if nxt is None or _char_style(nxt) != run_style:
                        break
                    x += 1
                text = "".join(
                    (buffer_line.get(i).data if buffer_line.get(i) is not None else " ")
                    for i in range(run_start, x)
                )
                line.append(text, style=run_style)
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
    """Pyte emits 'default', named colors, 'RRGGBB' truecolor (no '#'),
    and 1-3 digit 256-palette indices. Order matters: the hex check must
    precede the digit check ('999999' is both 6 digits and a hex string)."""
    if color is None or color == "default":
        return None
    if color in _PYTE_COLOR_ALIASES:
        return _PYTE_COLOR_ALIASES[color]
    if _HEX_RE.match(color):
        return color if color.startswith("#") else f"#{color}"
    if color.isdigit() and 0 <= int(color) <= 255:
        return f"color({color})"
    return None


def _char_style(char: Char) -> Style:
    fg = _resolve_color(char.fg)
    bg = _resolve_color(char.bg)
    try:
        return Style(
            color=fg, bgcolor=bg,
            bold=char.bold, italic=char.italics,
            underline=char.underscore,
            strike=char.strikethrough, blink=char.blink,
            reverse=char.reverse,
        )
    except (ValueError, TypeError):
        # Unknown color encoding from rich: drop color rather than crash.
        # Other exceptions (AttributeError from malformed Char, etc.) are
        # bugs; let them surface.
        return Style(
            bold=char.bold, italic=char.italics,
            underline=char.underscore,
            strike=char.strikethrough, blink=char.blink,
            reverse=char.reverse,
        )
