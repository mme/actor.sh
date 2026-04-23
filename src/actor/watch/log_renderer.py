"""Render log entries into a RichLog widget."""

from __future__ import annotations

from typing import Callable

from rich.padding import Padding
from rich.text import Text

from textual.widgets import RichLog

from ..interfaces import LogEntryKind
from .render_assistant import render_assistant
from .render_tool import render_tool, HIDDEN_TOOLS
from .render_user import render_user
from .types import ThemeColors


# Callable returning True when the build should abort. Builders check
# between entries; the default never cancels, so synchronous callers
# can ignore this entirely.
CancelCheck = Callable[[], bool]


def _never_cancelled() -> bool:
    return False


def render_log_entries(log: RichLog, entries: list, colors: ThemeColors) -> None:
    """Synchronous full rerender — kept for tests and any caller that
    doesn't want to deal with the two-phase build/apply dance.

    The watch app uses `build_log_renderables` + `apply_log_renderables`
    directly so the expensive markdown/table construction runs off the
    main thread."""
    renderables = build_log_renderables(entries, colors)
    if renderables is None:
        return  # unreachable with the default never-cancel check
    apply_log_renderables(log, renderables)


class _BufferingLog:
    """Collects ``log.write`` calls into a list for later replay.

    The per-entry renderers (render_user / render_assistant / render_tool
    / _render_thinking) write directly into whatever log-like object
    they're given. Pointing them at this buffer moves the entire
    build — markdown parsing, Table construction, Text assembly — off
    the main thread; the main thread then replays the captured writes
    onto the real RichLog. No Textual widget state is mutated here,
    which is what makes the off-thread build safe."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, dict]] = []

    def write(self, content: object, **kwargs) -> "_BufferingLog":
        self.calls.append((content, kwargs))
        return self


def build_log_renderables(
    entries: list,
    colors: ThemeColors,
    is_cancelled: CancelCheck = _never_cancelled,
) -> list[tuple[object, dict]] | None:
    """Render `entries` into a list of ``(renderable, kwargs)`` tuples
    without touching any widget. Safe to call from a worker thread.

    Same layout logic as the synchronous full rerender — including
    tool-pair resolution and blank-line separators — just writing to
    `_BufferingLog` instead of a RichLog.

    Returns None when `is_cancelled()` flips True mid-build. The
    caller (worker) takes that as a signal to drop its result and
    return silently — a newer build is running and will own the apply.
    """
    if is_cancelled():
        return None
    buf = _BufferingLog()
    if not entries:
        buf.write(Text("No logs yet", style="dim"))
        return buf.calls
    tool_results = _pair_tools(entries)
    _write_range(
        buf, entries, 0, tool_results, colors,
        already_rendered=False, is_cancelled=is_cancelled,
    )
    if is_cancelled():
        return None
    return buf.calls


def apply_log_renderables(
    log: RichLog, renderables: list[tuple[object, dict]],
) -> None:
    """Commit buffered renderables from `build_log_renderables` onto a
    real RichLog. Must run on the main thread — `clear` and `write`
    mutate widget state that the compositor reads."""
    log.clear()
    for content, kwargs in renderables:
        log.write(content, **kwargs)


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
    is_cancelled: CancelCheck = _never_cancelled,
) -> None:
    """Write entries[start_idx:] to the log, dispatching each kind to
    its dedicated renderer. `already_rendered` tells us whether the
    log already has output above us (so the first write in THIS pass
    needs a leading blank separator) or whether we're starting
    fresh.

    Cancellation is checked before each entry — a cooperative signal
    from the worker when a newer build has superseded this one. On
    cancel the function just returns; whatever was buffered so far
    will be discarded by the caller."""
    first_this_pass = True
    for idx in range(start_idx, len(entries)):
        if is_cancelled():
            return
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
