"""Render assistant messages in the log view."""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown as RichMarkdown, TextElement
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


class _PlainIndentedCode(TextElement):
    """Render ``code_block`` (indented code) tokens as plain text with
    whitespace preserved. Rich's default CodeBlock wraps them in a
    Syntax widget with padding, which produces a visible boxed frame
    for artistic poem indentation — not what Claude Code does.
    ``marked``'s ``formatToken`` ``code`` case emits ``token.text +
    EOL`` verbatim with no frame, so we mirror that here."""

    style_name = "none"

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield Text(str(self.text).rstrip("\n"))


class _ClaudeLikeMarkdown(RichMarkdown):
    """Rich Markdown with two deviations that bring it in line with how
    Claude Code's ``marked``-based renderer treats assistant text:

    - Indented code blocks render as plain text with whitespace
      preserved. markdown-it-py still parses them as ``code_block``
      tokens (so the content is captured), but we swap the element
      class so they aren't drawn inside a Syntax frame. Fenced
      (triple-backtick) blocks still use the default boxed rendering.
    - Strikethrough (``~~text~~``) stays off. Models often write
      ``~100`` to mean "approximately 100"; ``marked`` disables its
      ``del`` tokenizer for the same reason, and markdown-it-py has
      strikethrough disabled by default.
    """

    elements = {
        **RichMarkdown.elements,
        "code_block": _PlainIndentedCode,
    }

    def __init__(self, markup: str, **kwargs) -> None:
        super().__init__(markup, **kwargs)
        # Re-parse with tables enabled (matching Rich's default) but
        # keep indented-code parsing on — the element override above
        # is what makes it render without a frame.
        parser = MarkdownIt().enable("table")
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
