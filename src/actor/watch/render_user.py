"""Render user messages in the log view."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from textual.widgets import RichLog

from .types import ThemeColors


def render_user(log: RichLog, entry, colors: ThemeColors) -> None:
    """Render a user message with ❯ prefix and surface background.

    Foreground is pinned via `colors.user_fg` (white by default) rather
    than inherited from the active theme so flavored themes can't shift
    it away from readable contrast on the surface background."""
    bg = f"on {colors.surface}"
    fg = colors.user_fg
    table = Table(
        show_header=False,
        box=None,
        padding=0,
        expand=True,
        style=f"{fg} {bg}",
    )
    table.add_column(width=2, no_wrap=True, style=f"{fg} {bg}")
    table.add_column(ratio=1, style=f"{fg} {bg}")
    table.add_row(
        Text("❯ ", style=f"bold {fg} {bg}"),
        Text(entry.text, style=f"{fg} {bg}"),
    )
    log.write(table, expand=True)
