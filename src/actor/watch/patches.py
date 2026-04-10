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


def apply_patches() -> None:
    """Apply all monkey-patches. Call once at import time."""
    ANSIToTruecolor.truecolor_style = _patched_truecolor_style  # type: ignore[assignment]
