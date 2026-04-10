"""Render code diffs in Claude Code style using Rich renderables."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import NamedTuple

from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.token import Token
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.measure import Measurement
from rich.text import Text


class FullWidthText:
    """A Text renderable that pads to the full console width with a background style."""

    def __init__(self, text: Text, bg_style: str = "") -> None:
        self.text = text
        self.bg_style = bg_style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        text = self.text.copy()
        padding = width - text.cell_len
        if padding > 0 and self.bg_style:
            text.append(" " * padding, style=self.bg_style)
        yield text

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(self.text.cell_len, options.max_width)


# -- Colors ------------------------------------------------------------------

class DiffColors(NamedTuple):
    added_bg: str
    removed_bg: str
    added_word_bg: str
    removed_word_bg: str
    added_marker: str
    removed_marker: str
    context_fg: str
    line_num_fg: str
    dim: str

DARK_COLORS = DiffColors(
    added_bg="#022800",
    removed_bg="#3D0100",
    added_word_bg="#044700",
    removed_word_bg="#5C0200",
    added_marker="#50C850",
    removed_marker="#DC5A5A",
    context_fg="#888888",
    line_num_fg="#888888",
    dim="#666666",
)

LIGHT_COLORS = DiffColors(
    added_bg="#DCFFDC",
    removed_bg="#FFDCDC",
    added_word_bg="#B2FFB2",
    removed_word_bg="#FFC7C7",
    added_marker="#248A3D",
    removed_marker="#CF222E",
    context_fg="#666666",
    line_num_fg="#666666",
    dim="#999999",
)


# -- Word diff ---------------------------------------------------------------

def _tokenize(s: str) -> list[str]:
    """Split string into words, whitespace, and punctuation tokens."""
    return re.findall(r'\S+|\s+', s)


def _word_diff(old: str, new: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """Compute word-level diff. Returns (old_tokens, new_tokens) with changed flag."""
    old_tokens = _tokenize(old)
    new_tokens = _tokenize(new)

    sm = difflib.SequenceMatcher(None, old_tokens, new_tokens)

    # If more than 40% changed, don't do word highlighting
    ratio = sm.ratio()
    if ratio < 0.6:
        return [(old, True)], [(new, True)]

    old_result: list[tuple[str, bool]] = []
    new_result: list[tuple[str, bool]] = []

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            for t in old_tokens[i1:i2]:
                old_result.append((t, False))
            for t in new_tokens[j1:j2]:
                new_result.append((t, False))
        elif op == 'delete':
            for t in old_tokens[i1:i2]:
                old_result.append((t, True))
        elif op == 'insert':
            for t in new_tokens[j1:j2]:
                new_result.append((t, True))
        elif op == 'replace':
            for t in old_tokens[i1:i2]:
                old_result.append((t, True))
            for t in new_tokens[j1:j2]:
                new_result.append((t, True))

    return old_result, new_result


# -- Adjacent pair detection -------------------------------------------------

def _find_adjacent_pairs(markers: list[str]) -> list[tuple[int, int]]:
    """Find adjacent remove-then-add line pairs for word-level diff."""
    pairs = []
    i = 0
    while i < len(markers) - 1:
        if markers[i] == '-' and markers[i + 1] == '+':
            pairs.append((i, i + 1))
            i += 2
        else:
            i += 1
    return pairs


# -- Syntax highlighting -----------------------------------------------------

def _get_lexer(file_path: str):
    """Get Pygments lexer for a file path."""
    try:
        return get_lexer_for_filename(file_path, stripall=True)
    except Exception:
        return TextLexer()


def _highlight_line(line: str, lexer) -> Text:
    """Syntax-highlight a single line using Pygments."""
    from pygments import lex

    text = Text()
    for token_type, token_value in lex(line + "\n", lexer):
        style = _token_style(token_type)
        if token_value.endswith("\n"):
            token_value = token_value[:-1]
        if token_value:
            text.append(token_value, style=style)
    return text


def _token_style(token_type) -> str:
    """Map Pygments token to Rich style string."""
    TOKEN_STYLES = {
        Token.Keyword: "bold #C678DD",
        Token.Keyword.Constant: "#C678DD",
        Token.Keyword.Declaration: "bold #C678DD",
        Token.Keyword.Namespace: "#C678DD",
        Token.Keyword.Type: "#E5C07B",
        Token.Name.Builtin: "#61AFEF",
        Token.Name.Class: "bold #E5C07B",
        Token.Name.Decorator: "#E5C07B",
        Token.Name.Function: "#61AFEF",
        Token.Name.Function.Magic: "#61AFEF",
        Token.Literal.String: "#98C379",
        Token.Literal.String.Doc: "#98C379",
        Token.Literal.String.Escape: "#56B6C2",
        Token.Literal.String.Interpol: "#56B6C2",
        Token.Literal.String.Affix: "#C678DD",
        Token.Literal.Number: "#D19A66",
        Token.Literal.Number.Integer: "#D19A66",
        Token.Literal.Number.Float: "#D19A66",
        Token.Comment: "italic #5C6370",
        Token.Comment.Single: "italic #5C6370",
        Token.Comment.Multiline: "italic #5C6370",
        Token.Operator: "#56B6C2",
        Token.Operator.Word: "#C678DD",
        Token.Punctuation: "#ABB2BF",
        Token.Name: "#ABB2BF",
    }
    # Walk up the token hierarchy
    while token_type:
        if token_type in TOKEN_STYLES:
            return TOKEN_STYLES[token_type]
        token_type = token_type.parent
    return ""


# -- Main renderer -----------------------------------------------------------

def render_edit_diff(
    file_path: str,
    old_string: str,
    new_string: str,
    dark: bool = True,
    context_lines: int = 3,
) -> Group:
    """Render a file edit diff as a Rich Group renderable."""
    colors = DARK_COLORS if dark else LIGHT_COLORS
    lexer = _get_lexer(file_path)

    old_lines = old_string.splitlines(keepends=False)
    new_lines = new_string.splitlines(keepends=False)

    # Compute unified diff
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        lineterm='',
        n=context_lines,
    ))

    if not diff:
        return Group(Text("(no changes)", style="dim"))

    # Parse the unified diff into entries
    entries: list[dict] = []
    old_num = 0
    new_num = 0

    for line in diff:
        if line.startswith('@@'):
            # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
            match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
            if match:
                old_num = int(match.group(1))
                new_num = int(match.group(2))
                if entries:  # Add ellipsis between hunks
                    entries.append({"type": "ellipsis"})
            continue
        if line.startswith('---') or line.startswith('+++'):
            continue

        if line.startswith('+'):
            entries.append({"type": "add", "code": line[1:], "num": new_num, "marker": "+"})
            new_num += 1
        elif line.startswith('-'):
            entries.append({"type": "del", "code": line[1:], "num": old_num, "marker": "-"})
            old_num += 1
        else:
            code = line[1:] if line.startswith(' ') else line
            entries.append({"type": "ctx", "code": code, "num": new_num, "marker": " "})
            old_num += 1
            new_num += 1

    if not entries:
        return Group(Text("(no changes)", style="dim"))

    # Compute max line number width
    max_num = max((e.get("num", 0) for e in entries if e["type"] != "ellipsis"), default=0)
    num_width = len(str(max_num))

    # Compute word-diff ranges for adjacent del+add pairs
    markers = [e.get("marker", "") for e in entries]
    pairs = _find_adjacent_pairs(markers)
    word_diffs: dict[int, list[tuple[str, bool]]] = {}
    for del_idx, add_idx in pairs:
        old_tokens, new_tokens = _word_diff(entries[del_idx]["code"], entries[add_idx]["code"])
        word_diffs[del_idx] = old_tokens
        word_diffs[add_idx] = new_tokens

    # Count additions and removals
    added = sum(1 for e in entries if e["type"] == "add")
    removed = sum(1 for e in entries if e["type"] == "del")

    # Build output
    output: list = []

    # Header
    header = Text()
    header.append("⏺ ", style="bold")
    header.append("Edit", style="bold")
    header.append(f"({file_path})", style="dim")
    output.append(header)

    # Summary
    summary = Text("  ⎿  ", style="dim")
    if added and removed:
        summary.append(f"Added {added}, removed {removed} lines", style="dim")
    elif added:
        summary.append(f"Added {added} lines", style="dim")
    elif removed:
        summary.append(f"Removed {removed} lines", style="dim")
    output.append(summary)

    # Diff lines
    for i, entry in enumerate(entries):
        if entry["type"] == "ellipsis":
            output.append(Text(f"{'':>{num_width + 4}}...", style="dim"))
            continue

        line_text = Text()
        num = entry["num"]
        marker = entry["marker"]
        code = entry["code"]
        bg_style = ""

        # Line number
        num_str = f" {num:>{num_width}} "

        if entry["type"] == "add":
            bg_style = f"on {colors.added_bg}"
            line_text.append(num_str, style=f"{colors.added_marker} on {colors.added_bg}")
            line_text.append("+", style=f"bold {colors.added_marker} on {colors.added_bg}")

            if i in word_diffs:
                for token, changed in word_diffs[i]:
                    bg = colors.added_word_bg if changed else colors.added_bg
                    line_text.append(token, style=f"on {bg}")
            else:
                highlighted = _highlight_line(code, lexer)
                highlighted.stylize(f"on {colors.added_bg}")
                line_text.append_text(highlighted)

        elif entry["type"] == "del":
            bg_style = f"on {colors.removed_bg}"
            line_text.append(num_str, style=f"{colors.removed_marker} on {colors.removed_bg}")
            line_text.append("-", style=f"bold {colors.removed_marker} on {colors.removed_bg}")

            # Deleted lines: no syntax highlighting (matches Claude Code)
            if i in word_diffs:
                for token, changed in word_diffs[i]:
                    bg = colors.removed_word_bg if changed else colors.removed_bg
                    line_text.append(token, style=f"on {bg}")
            else:
                line_text.append(code, style=f"on {colors.removed_bg}")

        else:  # context
            line_text.append(num_str, style=f"{colors.line_num_fg}")
            line_text.append(" ", style="dim")
            highlighted = _highlight_line(code, lexer)
            highlighted.stylize("dim")
            line_text.append_text(highlighted)

        if bg_style:
            output.append(FullWidthText(line_text, bg_style))
        else:
            output.append(line_text)

    return Group(*output)


def render_write_diff(
    file_path: str,
    content: str,
    dark: bool = True,
) -> Group:
    """Render a file write as all-added lines."""
    return render_edit_diff(file_path, "", content, dark=dark)


def try_render_tool_diff(name: str, input_json: str, dark: bool = True) -> Group | None:
    """Try to render a tool call as a diff. Returns None if not applicable."""
    if name not in ("Edit", "Write"):
        return None

    try:
        data = json.loads(input_json)
    except (json.JSONDecodeError, TypeError):
        return None

    if name == "Edit":
        file_path = data.get("file_path", "unknown")
        old_string = data.get("old_string", "")
        new_string = data.get("new_string", "")
        if not old_string and not new_string:
            return None
        return render_edit_diff(file_path, old_string, new_string, dark=dark)

    elif name == "Write":
        file_path = data.get("file_path", "unknown")
        content = data.get("content", "")
        if not content:
            return None
        return render_write_diff(file_path, content, dark=dark)

    return None
