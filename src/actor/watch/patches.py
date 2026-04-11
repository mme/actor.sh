"""Monkey-patches for Textual internals."""

from __future__ import annotations

from functools import lru_cache

from rich.color import Color as RichColor, ColorType
from rich.style import Style as RichStyle
from textual.filter import ANSIToTruecolor


# -- Patch ANSIToTruecolor to preserve DEFAULT colors (SGR 49/39) -----------
# Without this, the filter converts RichColor("default") to a concrete RGB
# triplet, which prevents the terminal's own background from showing through.

_original_truecolor_style = ANSIToTruecolor.__dict__[
    "truecolor_style"
].__wrapped__


@lru_cache(1024)
def _patched_truecolor_style(
    self: ANSIToTruecolor, style: RichStyle, background: RichColor
) -> RichStyle:
    had_default_fg = style.color is not None and style.color.type == ColorType.DEFAULT
    had_default_bg = (
        style.bgcolor is not None and style.bgcolor.type == ColorType.DEFAULT
    )
    result = _original_truecolor_style(self, style, background)
    if had_default_fg or had_default_bg:
        overrides: dict[str, RichColor] = {}
        if had_default_fg:
            overrides["color"] = RichColor.parse("default")
        if had_default_bg:
            overrides["bgcolor"] = RichColor.parse("default")
        result = result + RichStyle(**overrides)
    return result


def _patch_markdown_list_numbers() -> None:
    """Patch Rich's ListItem to render '1.' instead of '1'."""
    from rich.markdown import ListItem
    from rich.segment import Segment
    from rich._loop import loop_first

    def render_number(self, console, options, number, last_number):
        number_width = len(str(last_number)) + 3
        render_options = options.update(width=options.max_width - number_width)
        lines = console.render_lines(self.elements, render_options, style=self.style)
        number_style = console.get_style("markdown.item.number", default="none")
        new_line = Segment("\n")
        padding = Segment(" " * number_width, number_style)
        numeral = Segment(f"{number}.".rjust(number_width - 1) + " ", number_style)
        for first, line in loop_first(lines):
            yield numeral if first else padding
            yield from line
            yield new_line

    ListItem.render_number = render_number


def apply_patches() -> None:
    """Apply all monkey-patches. Call once at import time."""
    ANSIToTruecolor.truecolor_style = _patched_truecolor_style  # type: ignore[assignment]
    _patch_markdown_list_numbers()
