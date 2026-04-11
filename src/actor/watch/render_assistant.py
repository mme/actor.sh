"""Render assistant messages in the log view."""

from __future__ import annotations

from rich.markdown import Markdown as RichMarkdown
from rich.table import Table
from rich.text import Text

from textual.widgets import RichLog


def render_assistant(log: RichLog, entry) -> None:
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
            RichMarkdown(text),
        )
        log.write(table, expand=True)
