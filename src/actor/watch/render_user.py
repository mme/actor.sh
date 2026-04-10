"""Render user messages in the log view."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from textual.widgets import RichLog

from .types import ThemeColors


def render_user(log: RichLog, entry, colors: ThemeColors) -> None:
    """Render a user message with ❯ prefix and surface background."""
    table = Table(
        show_header=False,
        box=None,
        padding=0,
        expand=True,
        style=f"on {colors.surface}",
    )
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1)
    table.add_row(
        Text("❯ ", style="bold"),
        Text(entry.text),
    )
    log.write(table, expand=True)
