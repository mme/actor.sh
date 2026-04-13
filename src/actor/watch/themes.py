"""Theme definitions for actor watch."""

from __future__ import annotations

from textual.theme import Theme

CLAUDE_DARK = Theme(
    name="claude-dark",
    primary="#B1B9F9",
    secondary="#D77757",
    warning="#FFC107",
    error="#FF6B80",
    success="#4EBA65",
    accent="#00CCCC",
    foreground="#FFFFFF",
    background="#1A1A1A",
    surface="#373737",
    panel="#2C323E",
    dark=True,
)

CLAUDE_LIGHT = Theme(
    name="claude-light",
    primary="#5769F7",
    secondary="#D77757",
    warning="#966C1E",
    error="#AB2B3F",
    success="#2C7A39",
    accent="#009999",
    foreground="#000000",
    background="#FFFFFF",
    surface="#F0F0F0",
    panel="#E8ECF4",
    dark=False,
)
