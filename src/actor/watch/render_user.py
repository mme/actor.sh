"""Render user messages in the log view."""

from __future__ import annotations

from rich.console import Group
from rich.padding import Padding
from rich.text import Text

from textual.widgets import RichLog

from .types import ThemeColors


def render_user(log: RichLog, entry, colors: ThemeColors) -> None:
    """Render a user message with ❯ prefix and surface background."""
    prompt = Text("❯ ", style="bold")
    lines = entry.text.split("\n")
    body = Text(lines[0])
    for line in lines[1:]:
        body.append("\n  " + line)
    log.write(
        Padding(
            Group(Text.assemble(prompt, body)),
            (0, 1, 0, 0),
            style=f"on {colors.surface}",
            expand=True,
        ),
        expand=True,
    )
