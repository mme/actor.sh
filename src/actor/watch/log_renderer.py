"""Render log entries into a RichLog widget."""

from __future__ import annotations

from rich.padding import Padding
from rich.text import Text

from textual.widgets import RichLog

from ..interfaces import LogEntryKind
from .render_assistant import render_assistant
from .render_tool import render_tool, HIDDEN_TOOLS
from .render_user import render_user
from .types import ThemeColors


def render_log_entries(log: RichLog, entries: list, colors: ThemeColors) -> None:
    """Render all log entries into a RichLog widget."""
    log.clear()

    if not entries:
        log.write(Text("No logs yet", style="dim"))
        return

    # Pair each TOOL_USE with its following TOOL_RESULT
    tool_results: dict[int, object] = {}
    for idx, entry in enumerate(entries):
        if entry.kind == LogEntryKind.TOOL_USE:
            for j in range(idx + 1, len(entries)):
                if entries[j].kind == LogEntryKind.TOOL_RESULT:
                    tool_results[idx] = entries[j]
                    break
                if entries[j].kind == LogEntryKind.TOOL_USE:
                    break

    first = True
    for idx, entry in enumerate(entries):
        # Skip invisible entries
        if entry.kind == LogEntryKind.TOOL_RESULT:
            continue
        if entry.kind == LogEntryKind.TOOL_USE and entry.name in HIDDEN_TOOLS:
            continue

        if not first:
            log.write(Text(""))
        first = False

        if entry.kind == LogEntryKind.USER:
            render_user(log, entry, colors)
        elif entry.kind == LogEntryKind.ASSISTANT:
            render_assistant(log, entry)
        elif entry.kind == LogEntryKind.THINKING:
            _render_thinking(log, entry)
        elif entry.kind == LogEntryKind.TOOL_USE:
            result = tool_results.get(idx)
            render_tool(log, entry, colors, result=result)


def _render_thinking(log: RichLog, entry) -> None:
    """Render a thinking message in dim italic."""
    log.write(Padding(
        Text(entry.text, style="dim italic"),
        (0, 1, 0, 2),
    ))
