"""Render code diffs in Claude Code style using Rich renderables."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Callable, NamedTuple, TYPE_CHECKING

from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.token import Token
from rich.console import Group
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from .helpers import FileDiff


# Callable returning True when the build should abort. Builders check
# between files and after each per-file render; the default never
# cancels, so synchronous callers can ignore this entirely. Mirrors
# the `CancelCheck` contract used by the logs renderer.
CancelCheck = Callable[[], bool]


def _never_cancelled() -> bool:
    return False




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


def _word_diff(
    old: str, new: str,
) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]] | None:
    """Compute word-level diff. Returns ``(old_tokens, new_tokens)``
    with per-token changed flags, or ``None`` when the lines are too
    dissimilar to make per-word highlighting useful (>40% of tokens
    changed). Callers should treat None as "skip the word-diff path
    and render this line with the standard line-bg + syntax
    highlighting"."""
    old_tokens = _tokenize(old)
    new_tokens = _tokenize(new)

    sm = difflib.SequenceMatcher(None, old_tokens, new_tokens)

    # If more than 40% changed, don't do word highlighting — the
    # whole line ends up smeared with `*_word_bg` which is louder
    # than the standard line-bg and obscures rather than highlights
    # the actual change. The caller falls back to the no-pair path.
    ratio = sm.ratio()
    if ratio < 0.6:
        return None

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
        return get_lexer_for_filename(file_path, stripall=False)
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
    style: str = "log",
) -> Group:
    """Render a file edit diff as a Rich Group renderable.

    Args:
        style: "log" for Claude Code tool call style (⏺ Edit header),
               "diff" for magit-style (file status + path).
    """
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
            match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)', line)
            if match:
                old_num = int(match.group(1))
                new_num = int(match.group(2))
                entries.append({"type": "hunk", "header": line})
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

    # Compute word-diff ranges for adjacent del+add pairs. Lines
    # too dissimilar (>40% changed) get a None back — those entries
    # never enter `word_diffs`, so the per-line render below falls
    # through to the standard syntax-highlighted line-bg path
    # instead of smearing the whole line with `*_word_bg`.
    markers = [e.get("marker", "") for e in entries]
    pairs = _find_adjacent_pairs(markers)
    word_diffs: dict[int, list[tuple[str, bool]]] = {}
    for del_idx, add_idx in pairs:
        result = _word_diff(
            entries[del_idx]["code"], entries[add_idx]["code"],
        )
        if result is None:
            continue
        old_tokens, new_tokens = result
        word_diffs[del_idx] = old_tokens
        word_diffs[add_idx] = new_tokens

    # Count additions and removals
    added = sum(1 for e in entries if e["type"] == "add")
    removed = sum(1 for e in entries if e["type"] == "del")

    # Build output
    output: list = []

    if style == "log":
        # Claude Code tool call style
        header = Text()
        header.append("⏺ ", style="bold")
        header.append("Edit", style="bold")
        header.append(f"({file_path})", style="dim")
        output.append(header)

        summary = Text("  ⎿  ", style="dim")
        if added and removed:
            summary.append(f"Added {added}, removed {removed} lines", style="dim")
        elif added:
            summary.append(f"Added {added} lines", style="dim")
        elif removed:
            summary.append(f"Removed {removed} lines", style="dim")
        output.append(summary)
    else:
        # Magit-style file header
        file_status = "new file" if not old_string else "deleted" if not new_string else "modified"
        header = Text()
        header.append(f"{file_status}   ", style="bold")
        header.append(file_path)
        stats = Text()
        if added:
            stats.append(f" +{added}", style=f"{colors.added_marker}")
        if removed:
            stats.append(f" -{removed}", style=f"{colors.removed_marker}")
        header.append_text(stats)
        output.append(header)

    # Diff table
    table = Table(
        show_header=False,
        box=None,
        padding=0,
        expand=True,
    )
    table.add_column(width=num_width + 2, no_wrap=True)  # line number
    table.add_column(width=1, no_wrap=True)               # marker
    table.add_column(ratio=1)                              # code

    for i, entry in enumerate(entries):
        if entry["type"] == "hunk":
            if style == "diff":
                if table.row_count:
                    output.append(table)
                    table = Table(show_header=False, box=None, padding=0, expand=True)
                    table.add_column(width=num_width + 2, no_wrap=True)
                    table.add_column(width=1, no_wrap=True)
                    table.add_column(ratio=1)
                output.append(Text(entry["header"], style=f"{colors.dim}"))
                output.append(Text(""))
            else:
                table.add_row(
                    Text("", style="dim"),
                    Text("", style="dim"),
                    Text("...", style="dim"),
                )
            continue

        num = entry["num"]
        code = entry["code"]
        num_text = Text(f"{num:>{num_width}} ", no_wrap=True)

        if entry["type"] == "add":
            bg = f"on {colors.added_bg}"
            num_text.stylize(f"{colors.added_marker} {bg}")
            marker_text = Text("+", style=f"bold {colors.added_marker} {bg}")

            if i in word_diffs:
                code_text = Text()
                for token, changed in word_diffs[i]:
                    wbg = f"on {colors.added_word_bg}" if changed else bg
                    code_text.append(token, style=wbg)
            else:
                code_text = _highlight_line(code, lexer)
                code_text.stylize(bg)

            table.add_row(num_text, marker_text, code_text, style=bg)

        elif entry["type"] == "del":
            bg = f"on {colors.removed_bg}"
            num_text.stylize(f"{colors.removed_marker} {bg}")
            marker_text = Text("-", style=f"bold {colors.removed_marker} {bg}")

            if i in word_diffs:
                code_text = Text()
                for token, changed in word_diffs[i]:
                    wbg = f"on {colors.removed_word_bg}" if changed else bg
                    code_text.append(token, style=wbg)
            else:
                code_text = Text(code, style=bg)

            table.add_row(num_text, marker_text, code_text, style=bg)

        else:  # context
            num_text.stylize(colors.line_num_fg)
            marker_text = Text(" ", style="dim")
            code_text = _highlight_line(code, lexer)
            code_text.stylize("dim")
            table.add_row(num_text, marker_text, code_text)

    output.append(table)

    return Group(*output)


def render_write_diff(
    file_path: str,
    content: str,
    dark: bool = True,
) -> Group:
    """Render a file write as all-added lines."""
    return render_edit_diff(file_path, "", content, dark=dark)


def iter_diff_renderables(
    files: list["FileDiff"],
    dark: bool,
    is_cancelled: CancelCheck = _never_cancelled,
):
    """Generator that yields ``(file_path, renderable, added, removed)``
    per file, suitable for streaming mounts onto the DIFF pane.

    The watch app's build worker iterates this generator and calls
    ``call_from_thread(_diff_append_file, ...)`` for each yielded
    tuple, so a 50-file diff appears progressively rather than after
    one giant final mount. Stays safe to call from a worker thread —
    no widget state is touched here.

    Cancellation: ``is_cancelled()`` is checked BEFORE rendering each
    file and AFTER rendering but BEFORE yielding. On cancel, the
    generator returns silently — the consumer should check
    ``is_cancelled()`` after the loop to distinguish "drained
    naturally" from "stopped mid-stream", and skip the
    finalize/commit step in the latter case.

    Per-file ± counts come straight from `FileDiff.added` /
    `FileDiff.removed`, which `compute_diff` populates by parsing
    `git diff` output (Stage 2)."""
    for fd in files:
        if is_cancelled():
            return
        renderable = render_edit_diff(
            fd.file_path, fd.old_content, fd.new_content,
            dark=dark, style="diff",
        )
        if is_cancelled():
            return
        yield fd.file_path, renderable, fd.added, fd.removed


def build_diff_renderables(
    files: list["FileDiff"],
    dark: bool,
    is_cancelled: CancelCheck = _never_cancelled,
) -> tuple[list, int, int] | None:
    """Synchronous full-build wrapper around `iter_diff_renderables`.

    Returns ``(parts, total_added, total_removed)`` on success or
    ``None`` when cancellation aborted the iteration. Each rendered
    file gets a trailing blank Text separator inside ``parts`` to
    match the previous mount layout for callers that still want a
    single `Group(*parts)` mount (e.g. tests, ad-hoc tools).

    The watch app's build worker bypasses this wrapper and consumes
    the iterator directly so it can mount per-file as renders complete
    — see `_build_diff_worker` in `actor.watch.app`."""
    parts: list = []
    total_added = 0
    total_removed = 0
    for _path, renderable, added, removed in iter_diff_renderables(
        files, dark, is_cancelled,
    ):
        parts.append(renderable)
        parts.append(Text(""))
        total_added += added
        total_removed += removed
    if is_cancelled():
        return None
    return parts, total_added, total_removed


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
