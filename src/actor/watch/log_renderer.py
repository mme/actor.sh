"""Render log entries into a RichLog widget."""

from __future__ import annotations

from rich.padding import Padding
from rich.text import Text

from textual.widgets import RichLog

from ..interfaces import LogEntryKind
from .render_assistant import render_assistant
from .render_tool import render_tool
from .render_user import render_user
from .types import ThemeColors



def render_log_entries(log: RichLog, entries: list, colors: ThemeColors) -> None:
    """Render all log entries into a RichLog widget.

    Args:
        log: The RichLog widget to write to.
        entries: List of LogEntry objects.
        colors: Resolved theme colors.
    """
    log.clear()

    if not entries:
        log.write(Text("No logs yet", style="dim"))
        return

    first = True
    for entry in entries:
        # Skip invisible entries
        if entry.kind == LogEntryKind.TOOL_RESULT:
            continue
        from .render_tool import HIDDEN_TOOLS
        if entry.kind == LogEntryKind.TOOL_USE and entry.name in HIDDEN_TOOLS:
            continue

        if not first:
            log.write(Text(""))
        first = False

        if entry.kind == LogEntryKind.USER:
            render_user(log, entry, colors)
        elif entry.kind == LogEntryKind.ASSISTANT:
            render_assistant(log, entry, colors)
        elif entry.kind == LogEntryKind.THINKING:
            _render_thinking(log, entry)
        elif entry.kind == LogEntryKind.TOOL_USE:
            render_tool(log, entry, colors)


def _render_thinking(log: RichLog, entry) -> None:
    """Render a thinking message in dim italic."""
    log.write(Padding(
        Text(entry.text, style="dim italic"),
        (0, 1, 0, 2),
    ))


