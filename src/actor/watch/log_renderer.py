"""Render log entries into a RichLog widget."""

from __future__ import annotations

from typing import NamedTuple

from rich.console import Group, RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.text import Text

from textual.widgets import RichLog

from ..interfaces import LogEntryKind
from .diff_render import try_render_tool_diff


class ThemeColors(NamedTuple):
    """Resolved theme colors needed for log rendering."""
    surface: str
    warning: str
    is_dark: bool


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
            _render_user(log, entry, colors)
        elif entry.kind == LogEntryKind.ASSISTANT:
            _render_assistant(log, entry)
        elif entry.kind == LogEntryKind.THINKING:
            _render_thinking(log, entry)
        elif entry.kind == LogEntryKind.TOOL_USE:
            _render_tool_use(log, entry, colors)
        elif entry.kind == LogEntryKind.TOOL_RESULT:
            _render_tool_result(log, entry)


def _render_user(log: RichLog, entry, colors: ThemeColors) -> None:
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


def _render_assistant(log: RichLog, entry) -> None:
    """Render an assistant message with ⏺ prefix and markdown."""
    text = entry.text.strip()
    if text:
        log.write(Padding(
            RichMarkdown("**⏺** " + text),
            (0, 0, 0, 0),
        ))


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
