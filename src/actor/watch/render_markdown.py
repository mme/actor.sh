"""Custom markdown rendering matching Claude Code's terminal style.

Claude Code uses minimal markdown styling:
- Inline code: permission color foreground, no background
- Headings: bold (h1 also italic+underline), no color
- Bold/italic: standard attributes, no color
- Links: blue
- Blockquotes: dim prefix, italic content
- Code blocks: syntax highlighted, no background
- No backgrounds on any markdown element
"""

from __future__ import annotations

from rich.markdown import Markdown
from rich.theme import Theme as RichTheme

# Claude Code markdown styles — dark theme
CLAUDE_MD_DARK = RichTheme({
    "markdown.code": f"#B1B9F9",  # permission color, no background
    "markdown.code_block": "none",  # no background, syntax highlighting handles colors
    "markdown.h1": "bold italic underline",
    "markdown.h1.border": "none",
    "markdown.h2": "bold",
    "markdown.h3": "bold",
    "markdown.h4": "bold",
    "markdown.h5": "bold",
    "markdown.h6": "dim",
    "markdown.em": "italic",
    "markdown.strong": "bold",
    "markdown.link": "blue",
    "markdown.link_url": "blue",
    "markdown.block_quote": "dim italic",
    "markdown.hr": "dim",
    "markdown.item.bullet": "bold",
    "markdown.paragraph": "none",
    "markdown.text": "none",
    "markdown.s": "strike",
})

# Claude Code markdown styles — light theme
CLAUDE_MD_LIGHT = RichTheme({
    "markdown.code": f"#5769F7",  # permission color light
    "markdown.code_block": "none",
    "markdown.h1": "bold italic underline",
    "markdown.h1.border": "none",
    "markdown.h2": "bold",
    "markdown.h3": "bold",
    "markdown.h4": "bold",
    "markdown.h5": "bold",
    "markdown.h6": "dim",
    "markdown.em": "italic",
    "markdown.strong": "bold",
    "markdown.link": "blue",
    "markdown.link_url": "blue",
    "markdown.block_quote": "dim italic",
    "markdown.hr": "dim",
    "markdown.item.bullet": "bold",
    "markdown.paragraph": "none",
    "markdown.text": "none",
    "markdown.s": "strike",
})


def get_md_theme(dark: bool = True) -> RichTheme:
    """Get the Rich theme for Claude-style markdown rendering."""
    return CLAUDE_MD_DARK if dark else CLAUDE_MD_LIGHT


class ThemedMarkdown:
    """A Rich renderable that renders Markdown with a custom theme."""

    def __init__(self, markup: str, dark: bool = True) -> None:
        self.markup = markup
        self.dark = dark

    def __rich_console__(self, console, options):
        from rich.console import Console
        theme = get_md_theme(self.dark)
        themed_console = Console(
            theme=theme,
            width=options.max_width,
            force_terminal=True,
            no_color=False,
        )
        md = Markdown(
            self.markup,
            code_theme="monokai" if self.dark else "default",
        )
        # Render with themed console and yield segments
        with themed_console.capture() as capture:
            themed_console.print(md, end="")
        from rich.text import Text
        yield from Text.from_ansi(capture.get()).render(console)

    def __rich_measure__(self, console, options):
        from rich.measure import Measurement
        return Measurement(1, options.max_width)
