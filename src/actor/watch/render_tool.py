"""Render tool calls in Claude Code style."""

from __future__ import annotations

import json

from rich.table import Table
from rich.text import Text

from .types import ThemeColors, ToolRenderContext, MAX_RESULT_LINES
from .diff_render import try_render_tool_diff


# -- Shared helpers ----------------------------------------------------------

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
    table.add_column(width=5, no_wrap=True)
    table.add_column(ratio=1)
    content = text if isinstance(text, Text) else Text(text, style="dim" if dim else "")
    table.add_row(Text("  ⎿ ", style="dim"), content)
    return table


def _truncate_result(result: str, max_lines: int = MAX_RESULT_LINES) -> str:
    """Truncate result to max_lines, appending +N lines if truncated."""
    if not result:
        return ""
    lines = result.strip().splitlines()
    if len(lines) <= max_lines:
        return result.strip()
    truncated = "\n".join(lines[:max_lines])
    remaining = len(lines) - max_lines
    return f"{truncated}\n... +{remaining} lines"


# -- Individual tool renderers -----------------------------------------------

def _truncate_command(command: str, max_lines: int = 2, max_chars: int = 160) -> str:
    """Truncate a command for display in the tool header."""
    original = command.strip()
    lines = original.splitlines()
    truncated = False
    if len(lines) > max_lines:
        original = "\n".join(lines[:max_lines])
        truncated = True
    if len(original) > max_chars:
        original = original[:max_chars]
        truncated = True
    return original.strip() + "…" if truncated else original.strip()


def _render_bash(ctx: ToolRenderContext) -> None:
    """Render Bash tool call with truncated output."""
    command = ctx.data.get("command", "")
    display = _truncate_command(command).replace("\n", " ")
    ctx.log.write(_tool_header("Bash", display, ctx.colors), expand=True)
    if ctx.result:
        ctx.log.write(_connector(_truncate_result(ctx.result)), expand=True)
    else:
        ctx.log.write(_connector("(No output)"), expand=True)


def _render_read(ctx: ToolRenderContext) -> None:
    """Render Read tool call."""
    file_path = ctx.data.get("file_path", "")
    suffix = ""
    if ctx.data.get("offset") and ctx.data.get("limit"):
        suffix = f", lines {ctx.data['offset']}-{ctx.data['offset'] + ctx.data['limit']}"
    elif ctx.data.get("pages"):
        suffix = f", pages {ctx.data['pages']}"
    ctx.log.write(_tool_header("Read", file_path + suffix, ctx.colors), expand=True)
    if ctx.result:
        # Show "Read N lines" summary
        line_count = len(ctx.result.splitlines())
        ctx.log.write(_connector(Text.assemble(
            "Read ", (str(line_count), "bold"), " lines",
        )), expand=True)


def _render_write(ctx: ToolRenderContext) -> None:
    """Render Write tool call."""
    file_path = ctx.data.get("file_path", "")
    content = ctx.data.get("content", "")
    line_count = len(content.splitlines()) if content else 0
    ctx.log.write(_tool_header("Write", file_path, ctx.colors), expand=True)
    ctx.log.write(_connector(Text.assemble(
        "Wrote ", (str(line_count), "bold"), " lines",
    )), expand=True)


def _render_edit(ctx: ToolRenderContext) -> None:
    """Render Edit tool call with diff."""
    file_path = ctx.data.get("file_path", "")
    diff = try_render_tool_diff("Edit", json.dumps(ctx.data), dark=ctx.colors.is_dark)
    if diff:
        ctx.log.write(diff, expand=True)
    else:
        ctx.log.write(_tool_header("Update", file_path, ctx.colors), expand=True)


def _render_glob(ctx: ToolRenderContext) -> None:
    """Render Glob/Search tool call."""
    pattern = ctx.data.get("pattern", "")
    path = ctx.data.get("path", "")
    params = f'pattern: "{pattern}"'
    if path:
        params += f', path: "{path}"'
    ctx.log.write(_tool_header("Search", params, ctx.colors), expand=True)
    if ctx.result:
        file_count = len(ctx.result.strip().splitlines())
        ctx.log.write(_connector(Text.assemble(
            "Found ", (str(file_count), "bold"), " files",
        )), expand=True)


def _render_grep(ctx: ToolRenderContext) -> None:
    """Render Grep/Search tool call."""
    pattern = ctx.data.get("pattern", "")
    path = ctx.data.get("path", "")
    params = f'pattern: "{pattern}"'
    if path:
        params += f', path: "{path}"'
    ctx.log.write(_tool_header("Search", params, ctx.colors), expand=True)
    if ctx.result:
        line_count = len(ctx.result.strip().splitlines())
        ctx.log.write(_connector(Text.assemble(
            "Found ", (str(line_count), "bold"), " results",
        )), expand=True)


def _render_web_fetch(ctx: ToolRenderContext) -> None:
    """Render WebFetch tool call."""
    url = ctx.data.get("url", "")
    ctx.log.write(_tool_header("WebFetch", url, ctx.colors), expand=True)


def _render_web_search(ctx: ToolRenderContext) -> None:
    """Render WebSearch tool call."""
    query = ctx.data.get("query", "")
    ctx.log.write(_tool_header("WebSearch", f'"{query}"', ctx.colors), expand=True)


def _render_agent(ctx: ToolRenderContext) -> None:
    """Render Agent tool call."""
    description = ctx.data.get("description", ctx.data.get("prompt", ""))
    if len(description) > 100:
        description = description[:97] + "..."
    ctx.log.write(_tool_header("Agent", description, ctx.colors), expand=True)


def _render_fallback(ctx: ToolRenderContext) -> None:
    """Render unknown tool call with generic display."""
    display = ""
    for key in ("file_path", "path", "name", "command", "query", "url", "pattern", "prompt", "description"):
        if key in ctx.data:
            val = str(ctx.data[key])
            if len(val) > 100:
                val = val[:97] + "..."
            display = val
            break
    if not display and ctx.data:
        display = str(ctx.data)[:100]
    ctx.log.write(_tool_header(ctx.name, display, ctx.colors), expand=True)


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

HIDDEN_TOOLS = {"TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "ToolSearch"}


def _parse_input(input_json: str) -> dict:
    try:
        return json.loads(input_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def render_tool(log, entry, colors: ThemeColors, result=None) -> None:
    """Render a tool use entry in Claude Code style."""
    name = entry.name

    if name in HIDDEN_TOOLS:
        return

    ctx = ToolRenderContext(
        log=log,
        name=name,
        data=_parse_input(entry.input),
        colors=colors,
        result=result.content if result else "",
    )

    renderer = TOOL_RENDERERS.get(name)
    if renderer:
        renderer(ctx)
    else:
        _render_fallback(ctx)
