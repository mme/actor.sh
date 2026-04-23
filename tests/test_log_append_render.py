"""Append-only log rendering — only the new tail of entries gets
written to the RichLog when the list grows. Full rerender is the
fallback when tool pairings or width/actor changes make the append
path unsafe."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from actor.interfaces import LogEntry, LogEntryKind
from actor.watch.log_renderer import (
    append_log_entries,
    render_log_entries,
)
from actor.watch.types import ThemeColors


def _colors() -> ThemeColors:
    return ThemeColors(
        surface="#373737",
        warning="#FFC107",
        is_dark=True,
    )


def _user(text: str) -> LogEntry:
    return LogEntry(kind=LogEntryKind.USER, text=text)


def _assistant(text: str) -> LogEntry:
    return LogEntry(kind=LogEntryKind.ASSISTANT, text=text)


def _tool_use(name: str, input: str = "{}") -> LogEntry:
    return LogEntry(kind=LogEntryKind.TOOL_USE, name=name, input=input)


def _tool_result(content: str = "ok") -> LogEntry:
    return LogEntry(kind=LogEntryKind.TOOL_RESULT, content=content)


class TestAppendLogEntries(unittest.TestCase):
    """append_log_entries writes only entries[prior_count:], skipping
    work on the already-rendered prefix."""

    def test_append_skips_already_rendered_prefix(self):
        log = MagicMock()
        log.lines = [object(), object()]  # simulate non-empty log
        entries = [_user("a"), _assistant("b"), _user("c"), _user("d")]

        with patch("actor.watch.log_renderer.render_user") as ru, \
             patch("actor.watch.log_renderer.render_assistant") as ra:
            append_log_entries(log, entries, prior_count=2, colors=_colors())

        # Only c and d were rendered.
        self.assertEqual(ru.call_count, 2)
        self.assertEqual(ra.call_count, 0)

    def test_append_noop_when_prior_equals_total(self):
        log = MagicMock()
        entries = [_user("a")]
        with patch("actor.watch.log_renderer.render_user") as ru:
            append_log_entries(log, entries, prior_count=1, colors=_colors())
        self.assertEqual(ru.call_count, 0)

    def test_append_leading_separator_when_log_has_prior_content(self):
        log = MagicMock()
        log.lines = [object()]  # already has content
        entries = [_user("old"), _user("new")]
        with patch("actor.watch.log_renderer.render_user"):
            append_log_entries(log, entries, prior_count=1, colors=_colors())

        # First log.write is the blank separator before the new entry.
        first_write = log.write.call_args_list[0]
        self.assertEqual(str(first_write.args[0]), "")

    def test_append_no_leading_separator_when_log_empty(self):
        log = MagicMock()
        log.lines = []  # empty
        entries = [_user("first-render")]
        writes: list = []
        log.write.side_effect = lambda *a, **kw: writes.append(a[0] if a else None)
        with patch("actor.watch.log_renderer.render_user") as ru:
            append_log_entries(log, entries, prior_count=0, colors=_colors())
        # No blank-line separator should fire before render_user is called.
        # render_user wasn't patched to write into `writes`, so all
        # entries of writes are direct log.write calls from
        # append_log_entries itself (i.e. the separator).
        self.assertEqual(len(writes), 0)
        self.assertEqual(ru.call_count, 1)

    def test_tool_result_in_tail_still_pairs_with_tool_use_in_tail(self):
        log = MagicMock()
        log.lines = [object()]
        entries = [
            _user("spawn tool"),
            _tool_use("bash", '{"cmd":"ls"}'),
            _tool_result("a\nb\n"),
        ]
        with patch("actor.watch.log_renderer.render_tool") as rt, \
             patch("actor.watch.log_renderer.render_user"):
            append_log_entries(log, entries, prior_count=1, colors=_colors())
        # render_tool called once for the tool_use, with the tool_result
        # passed as its paired result. tool_result itself isn't rendered
        # independently.
        self.assertEqual(rt.call_count, 1)
        _, kwargs = rt.call_args
        self.assertIsNotNone(kwargs.get("result"))

    def test_hidden_tools_skipped_in_tail(self):
        from actor.watch.render_tool import HIDDEN_TOOLS
        log = MagicMock()
        log.lines = [object()]
        hidden_name = next(iter(HIDDEN_TOOLS)) if HIDDEN_TOOLS else "TodoWrite"
        entries = [_user("a"), _tool_use(hidden_name)]
        with patch("actor.watch.log_renderer.render_tool") as rt:
            append_log_entries(log, entries, prior_count=1, colors=_colors())
        self.assertEqual(rt.call_count, 0)


class TestRenderLogEntriesFullStillWorks(unittest.TestCase):
    """After the refactor that extracted _write_range + _pair_tools,
    the full-render path should behave identically for any fixed
    input."""

    def test_full_render_clears_then_writes_all(self):
        log = MagicMock()
        entries = [_user("a"), _assistant("b")]
        with patch("actor.watch.log_renderer.render_user") as ru, \
             patch("actor.watch.log_renderer.render_assistant") as ra:
            render_log_entries(log, entries, _colors())
        log.clear.assert_called_once()
        self.assertEqual(ru.call_count, 1)
        self.assertEqual(ra.call_count, 1)

    def test_full_render_empty_entries_shows_placeholder(self):
        log = MagicMock()
        render_log_entries(log, [], _colors())
        log.clear.assert_called_once()
        # One write: the "No logs yet" placeholder.
        self.assertEqual(log.write.call_count, 1)


if __name__ == "__main__":
    unittest.main()
