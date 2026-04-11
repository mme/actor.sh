"""Render assistant messages in the log view."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from textual.widgets import RichLog

from .render_markdown import ThemedMarkdown
from .types import ThemeColors


def render_assistant(log: RichLog, entry, colors: ThemeColors) -> None:
    """Render an assistant message with ⏺ prefix and markdown."""
    text = entry.text.strip()
    if text:
        table = Table(
            show_header=False,
            box=None,
            padding=0,
            expand=True,
        )
        table.add_column(width=2, no_wrap=True)
        table.add_column(ratio=1)
        table.add_row(
            Text("⏺ ", style="bold"),
            ThemedMarkdown(text, dark=colors.is_dark),
        )
        log.write(table, expand=True)
