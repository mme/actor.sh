"""Render log entries into a RichLog widget."""

from __future__ import annotations

from rich.padding import Padding
from rich.text import Text

from textual.widgets import RichLog

from ..interfaces import LogEntryKind
from .diff_render import try_render_tool_diff
from .render_assistant import render_assistant
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

    for entry in entries:
        log.write(Text(""))

        if entry.kind == LogEntryKind.USER:
            render_user(log, entry, colors)
        elif entry.kind == LogEntryKind.ASSISTANT:
            render_assistant(log, entry)
        elif entry.kind == LogEntryKind.THINKING:
            _render_thinking(log, entry)
        elif entry.kind == LogEntryKind.TOOL_USE:
            _render_tool_use(log, entry, colors)
        elif entry.kind == LogEntryKind.TOOL_RESULT:
            _render_tool_result(log, entry)




def _render_thinking(log: RichLog, entry) -> None:
    """Render a thinking message in dim italic."""
    log.write(Padding(
        Text(entry.text, style="dim italic"),
        (0, 1, 0, 2),
    ))


def _render_tool_use(log: RichLog, entry, colors: ThemeColors) -> None:
    """Render a tool use — diff for Edit/Write, fallback for others."""
    diff_renderable = try_render_tool_diff(entry.name, entry.input, dark=colors.is_dark)
    if diff_renderable:
        log.write(diff_renderable, expand=True)
    else:
        header = Text(f"  ⚡ {entry.name}", style=f"bold {colors.warning}")
        log.write(header)
        if entry.input:
            body = entry.input[:200] + ("..." if len(entry.input) > 200 else "")
            log.write(Padding(
                Text(body, style="dim"),
                (0, 1, 0, 4),
            ))


def _render_tool_result(log: RichLog, entry) -> None:
    """Render a tool result in dim text."""
    if entry.content:
        body = entry.content[:300] + ("..." if len(entry.content) > 300 else "")
        log.write(Padding(
            Text(body, style="dim"),
            (0, 1, 0, 4),
        ))
