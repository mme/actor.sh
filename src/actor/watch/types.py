"""Shared types for the watch package."""

from __future__ import annotations

from typing import NamedTuple


class ThemeColors(NamedTuple):
    """Resolved theme colors needed for log rendering."""
    surface: str
    warning: str
    is_dark: bool
    success_color: str = "#4EBA65"
    error_color: str = "#FF6B80"
    inactive: str = "#999999"
