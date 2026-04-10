"""Render assistant messages in the log view."""

from __future__ import annotations

from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding

from textual.widgets import RichLog


def render_assistant(log: RichLog, entry) -> None:
    """Render an assistant message with ⏺ prefix and markdown."""
    text = entry.text.strip()
    if text:
        log.write(Padding(
            RichMarkdown("**⏺** " + text),
            (0, 0, 0, 0),
        ))
