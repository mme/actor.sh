"""Render tool calls in Claude Code style."""

from __future__ import annotations

import json

from rich.table import Table
from rich.text import Text

from textual.widgets import RichLog

from .types import ThemeColors
from .diff_render import try_render_tool_diff


# -- Tool header -------------------------------------------------------------

def _tool_header(name: str, params: str, colors: ThemeColors, success: bool = True) -> Table:
    """Render a tool header: ⏺ ToolName(params)"""
    color = colors.success_color if success else colors.error_color
    table = Table(show_header=False, box=None, padding=0, expand=True)
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1)
    table.add_row(
        Text("⏺ ", style=f"{color}"),
        Text.assemble((name, "bold"), (f"({params})", "dim") if params else ("", "")),
    )
    return table


def _connector(text: str | Text, dim: bool = True) -> Table:
    """Render a connector line: ⎿  content"""
    table = Table(show_header=False, box=None, padding=0, expand=True)
    table.add_column(width=6, no_wrap=True)
    table.add_column(ratio=1)
    content = text if isinstance(text, Text) else Text(text, style="dim" if dim else "")
    table.add_row(Text("  ⎿  ", style="dim"), content)
    return table


# -- Tool-specific parsers ---------------------------------------------------

def _parse_input(input_json: str) -> dict:
    """Safely parse tool input JSON."""
    try:
        return json.loads(input_json)
    except (json.JSONDecodeError, TypeError):
        return {}


# -- Individual tool renderers -----------------------------------------------

def _render_bash(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render Bash tool call."""
    command = data.get("command", "")
    description = data.get("description", "")
    display = description or command
    # Truncate long commands
    if len(display) > 160:
        display = display[:157] + "..."
    log.write(_tool_header("Bash", display, colors), expand=True)


def _render_read(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render Read tool call."""
    file_path = data.get("file_path", "")
    suffix = ""
    if data.get("offset") and data.get("limit"):
        suffix = f", lines {data['offset']}-{data['offset'] + data['limit']}"
    elif data.get("pages"):
        suffix = f", pages {data['pages']}"
    log.write(_tool_header("Read", file_path + suffix, colors), expand=True)


def _render_write(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render Write tool call with file content preview."""
    file_path = data.get("file_path", "")
    content = data.get("content", "")
    line_count = len(content.splitlines()) if content else 0
    log.write(_tool_header("Write", file_path, colors), expand=True)
    log.write(_connector(Text.assemble(
        "Wrote ", (str(line_count), "bold"), " lines",
    )), expand=True)


def _render_edit(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render Edit tool call with diff."""
    file_path = data.get("file_path", "")
    diff = try_render_tool_diff("Edit", json.dumps(data), dark=colors.is_dark)
    if diff:
        log.write(diff, expand=True)
    else:
        log.write(_tool_header("Update", file_path, colors), expand=True)


def _render_glob(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render Glob/Search tool call."""
    pattern = data.get("pattern", "")
    path = data.get("path", "")
    params = f'pattern: "{pattern}"'
    if path:
        params += f', path: "{path}"'
    log.write(_tool_header("Search", params, colors), expand=True)


def _render_grep(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render Grep/Search tool call."""
    pattern = data.get("pattern", "")
    path = data.get("path", "")
    params = f'pattern: "{pattern}"'
    if path:
        params += f', path: "{path}"'
    log.write(_tool_header("Search", params, colors), expand=True)


def _render_web_fetch(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render WebFetch tool call."""
    url = data.get("url", "")
    log.write(_tool_header("WebFetch", url, colors), expand=True)


def _render_web_search(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render WebSearch tool call."""
    query = data.get("query", "")
    log.write(_tool_header("WebSearch", f'"{query}"', colors), expand=True)


def _render_agent(log: RichLog, data: dict, colors: ThemeColors) -> None:
    """Render Agent tool call."""
    description = data.get("description", data.get("prompt", ""))
    if len(description) > 100:
        description = description[:97] + "..."
    log.write(_tool_header("Agent", description, colors), expand=True)


def _render_fallback(log: RichLog, name: str, data: dict, colors: ThemeColors) -> None:
    """Render unknown tool call with generic display."""
    # Try to find a meaningful parameter to show
    display = ""
    for key in ("file_path", "path", "name", "command", "query", "url", "pattern", "prompt", "description"):
        if key in data:
            val = str(data[key])
            if len(val) > 100:
                val = val[:97] + "..."
            display = val
            break
    if not display and data:
        display = str(data)[:100]
    log.write(_tool_header(name, display, colors), expand=True)


# -- Tool renderer dispatch --------------------------------------------------

TOOL_RENDERERS = {
    "Bash": _render_bash,
    "Read": _render_read,
    "Write": _render_write,
    "Edit": _render_edit,
    "Glob": _render_glob,
    "Grep": _render_grep,
    "WebFetch": _render_web_fetch,
    "WebSearch": _render_web_search,
    "Agent": _render_agent,
}

# Tools to hide entirely
HIDDEN_TOOLS = {"TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "ToolSearch"}


def render_tool(log: RichLog, entry, colors: ThemeColors) -> None:
    """Render a tool use entry in Claude Code style."""
    name = entry.name

    # Hide invisible tools
    if name in HIDDEN_TOOLS:
        return

    data = _parse_input(entry.input)

    # Try specific renderer
    renderer = TOOL_RENDERERS.get(name)
    if renderer:
        renderer(log, data, colors)
    else:
        _render_fallback(log, name, data, colors)
