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
    """Full rerender of all log entries. Clears the widget first.

    Used when the actor changed, width changed, or the incoming entry
    list diverges from what's already in the log (e.g. a tool_result
    landed that pairs with a tool_use we already rendered without
    one). For the common "growing tail" case, prefer
    append_log_entries instead — it scales with the delta rather than
    the total."""
    log.clear()

    if not entries:
        log.write(Text("No logs yet", style="dim"))
        return

    tool_results = _pair_tools(entries)
    _write_range(log, entries, 0, tool_results, colors, already_rendered=False)


def append_log_entries(
    log: RichLog, entries: list, prior_count: int, colors: ThemeColors,
) -> None:
    """Append entries[prior_count:] to a log that already holds
    render output for entries[:prior_count].

    Relies on the caller having verified that the append path is safe
    — specifically, that no tool_result in the tail pairs with an
    already-rendered tool_use (which RichLog can't patch in place).
    Tool pairings are recomputed across the full list so a tool_use
    and tool_result that are BOTH in the tail pair correctly at
    append time.

    The blank-line separator between entries follows the same rule as
    the full renderer — emitted before every rendered entry except
    the very first visible one in the log."""
    if prior_count >= len(entries):
        return
    tool_results = _pair_tools(entries)
    _write_range(
        log, entries, prior_count, tool_results, colors,
        already_rendered=len(log.lines) > 0,
    )


def _pair_tools(entries: list) -> dict[int, object]:
    """Scan forward from each tool_use for its next tool_result, same
    pairing rule the renderers have always used. Returns a dict
    keyed by tool_use entry index."""
    tool_results: dict[int, object] = {}
    for idx, entry in enumerate(entries):
        if entry.kind == LogEntryKind.TOOL_USE:
            for j in range(idx + 1, len(entries)):
                if entries[j].kind == LogEntryKind.TOOL_RESULT:
                    tool_results[idx] = entries[j]
                    break
                if entries[j].kind == LogEntryKind.TOOL_USE:
                    break
    return tool_results


def _write_range(
    log: RichLog,
    entries: list,
    start_idx: int,
    tool_results: dict[int, object],
    colors: ThemeColors,
    already_rendered: bool,
) -> None:
    """Write entries[start_idx:] to the log, dispatching each kind to
    its dedicated renderer. `already_rendered` tells us whether the
    log already has output above us (so the first write in THIS pass
    needs a leading blank separator) or whether we're starting
    fresh."""
    first_this_pass = True
    for idx in range(start_idx, len(entries)):
        entry = entries[idx]
        if entry.kind == LogEntryKind.TOOL_RESULT:
            continue
        if entry.kind == LogEntryKind.TOOL_USE and entry.name in HIDDEN_TOOLS:
            continue

        needs_separator = already_rendered or not first_this_pass
        if needs_separator:
            log.write(Text(""))
        first_this_pass = False
        already_rendered = True

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
