"""Shared types for the watch package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from textual.widgets import RichLog


class ThemeColors(NamedTuple):
    """Resolved theme colors needed for log rendering."""
    surface: str
    warning: str
    is_dark: bool
    success_color: str = "#4EBA65"
    error_color: str = "#FF6B80"
    inactive: str = "#999999"
    # User-message FG is intentionally theme-agnostic: always white on
    # the surface background. Pulling the theme's foreground here would
    # produce unreadable contrast when flavored themes (e.g. omarchy)
    # shift the FG away from pure white.
    user_fg: str = "#FFFFFF"


MAX_RESULT_LINES = 3


@dataclass
class ToolRenderContext:
    """Everything a tool renderer needs."""
    log: RichLog
    name: str
    data: dict
    colors: ThemeColors
    result: str = ""
