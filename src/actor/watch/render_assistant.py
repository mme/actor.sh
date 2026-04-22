"""Render assistant messages in the log view."""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from rich.markdown import Markdown as RichMarkdown
from rich.table import Table
from rich.text import Text

from textual.widgets import RichLog


# Single newlines inside a markdown paragraph are "soft breaks" in
# markdown-it-py (Rich's underlying parser) and render as a space —
# stanzas of a poem collapse into one long wrapped paragraph. Claude
# Code uses `marked` which preserves intra-paragraph newlines verbatim,
# so the same assistant text renders with each line distinct in the
# interactive view. Normalize by upgrading every single `\n` (i.e.,
# one that isn't part of a `\n\n` paragraph break) into a markdown
# hard break (`  \n`) so the log view matches the interactive view.
_SOFT_BREAK_RE = re.compile(r"(?<!\n)\n(?!\n)")


def _softbreaks_to_hardbreaks(text: str) -> str:
    return _SOFT_BREAK_RE.sub("  \n", text)


class _ClaudeLikeMarkdown(RichMarkdown):
    """Rich Markdown with two deviations that bring it in line with how
    Claude Code's `marked`-based renderer treats assistant text:

    - Indented code blocks (4+ leading spaces after a blank line) are
      disabled. markdown-it-py otherwise turns artistic poem
      indentation like ``                              The atlas grows.``
      into a fenced-looking code block; `marked` treats that same input
      as an ongoing paragraph.
    - Strikethrough (``~~text~~``) stays off. Models regularly write
      ``~100`` to mean "approximately 100"; `marked` disables
      strikethrough for the same reason.
    """

    def __init__(self, markup: str, **kwargs) -> None:
        super().__init__(markup, **kwargs)
        parser = MarkdownIt().enable("table").disable("code")
        self.parsed = parser.parse(markup)


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
            _ClaudeLikeMarkdown(_softbreaks_to_hardbreaks(text)),
        )
        log.write(table, expand=True)
