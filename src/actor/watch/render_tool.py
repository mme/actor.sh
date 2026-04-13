"""Render tool calls in Claude Code style."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from rich.table import Table
from rich.text import Text

from textual.widgets import RichLog

from .types import ThemeColors
from .diff_render import try_render_tool_diff

MAX_COMMAND_DISPLAY_CHARS = 160
MAX_COMMAND_DISPLAY_LINES = 2
MAX_OUTPUT_LINES = 3


# -- Render context ----------------------------------------------------------

@dataclass
class ToolRenderContext:
    """Everything a tool renderer needs."""
    log: RichLog
    name: str
    data: dict
    colors: ThemeColors
    result: str


# -- Shared helpers ----------------------------------------------------------

def tool_header(name: str, params: str, ctx: ToolRenderContext) -> Table:
    """Render a tool header: ⏺ ToolName(params)"""
    table = Table(show_header=False, box=None, padding=0, expand=True)
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1)
    table.add_row(
        Text("⏺ ", style=ctx.colors.success_color),
        Text.assemble((name, "bold"), (f"({params})", "dim") if params else ("", "")),
    )
    return table


def connector(text: str | Text, dim: bool = True) -> Table:
    """Render a connector line: ⎿  content"""
    table = Table(show_header=False, box=None, padding=0, expand=True)
    table.add_column(width=4, no_wrap=True)
    table.add_column(ratio=1)
    content = text if isinstance(text, Text) else Text(text, style="dim" if dim else "")
    table.add_row(Text("  ⎿ ", style="dim"), content)
    return table


def truncate_output(text: str, max_lines: int = MAX_OUTPUT_LINES) -> str:
    """Truncate output to max_lines, appending +N lines if truncated."""
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return text.strip()
    truncated = "\n".join(lines[:max_lines])
    remaining = len(lines) - max_lines
    return f"{truncated}\n... +{remaining} lines"


# -- Individual tool renderers -----------------------------------------------

def render_bash(ctx: ToolRenderContext) -> None:
    """Render Bash tool call with truncated output."""
    command = ctx.data.get("command", "")
    if len(command) > MAX_COMMAND_DISPLAY_CHARS:
        command = command[:MAX_COMMAND_DISPLAY_CHARS] + "…"
    ctx.log.write(tool_header("Bash", command, ctx), expand=True)
    if ctx.result:
        ctx.log.write(connector(truncate_output(ctx.result), dim=False), expand=True)
    else:
        ctx.log.write(connector("(No output)"), expand=True)


def render_read(ctx: ToolRenderContext) -> None:
    """Render Read tool call."""
    file_path = ctx.data.get("file_path", "")
    suffix = ""
    if ctx.data.get("offset") and ctx.data.get("limit"):
        suffix = f", lines {ctx.data['offset']}-{ctx.data['offset'] + ctx.data['limit']}"
    elif ctx.data.get("pages"):
        suffix = f", pages {ctx.data['pages']}"
    ctx.log.write(tool_header("Read", file_path + suffix, ctx), expand=True)
    if ctx.result:
        # Count lines in result
        line_count = len(ctx.result.strip().splitlines())
        ctx.log.write(connector(Text.assemble(
            "Read ", (str(line_count), "bold"), " lines",
        )), expand=True)


def render_write(ctx: ToolRenderContext) -> None:
    """Render Write tool call."""
    file_path = ctx.data.get("file_path", "")
    content = ctx.data.get("content", "")
    line_count = len(content.splitlines()) if content else 0
    ctx.log.write(tool_header("Write", file_path, ctx), expand=True)
    ctx.log.write(connector(Text.assemble(
        "Wrote ", (str(line_count), "bold"), " lines",
    )), expand=True)


def render_edit(ctx: ToolRenderContext) -> None:
    """Render Edit tool call with diff."""
    file_path = ctx.data.get("file_path", "")
    diff = try_render_tool_diff("Edit", json.dumps(ctx.data), dark=ctx.colors.is_dark)
    if diff:
        ctx.log.write(diff, expand=True)
    else:
        ctx.log.write(tool_header("Update", file_path, ctx), expand=True)


def render_glob(ctx: ToolRenderContext) -> None:
    """Render Glob/Search tool call."""
    pattern = ctx.data.get("pattern", "")
    path = ctx.data.get("path", "")
    params = f'pattern: "{pattern}"'
    if path:
        params += f', path: "{path}"'
    ctx.log.write(tool_header("Search", params, ctx), expand=True)
    if ctx.result:
        lines = ctx.result.strip().splitlines()
        ctx.log.write(connector(Text.assemble(
            "Found ", (str(len(lines)), "bold"), " files",
        )), expand=True)


def render_grep(ctx: ToolRenderContext) -> None:
    """Render Grep/Search tool call."""
    pattern = ctx.data.get("pattern", "")
    path = ctx.data.get("path", "")
    params = f'pattern: "{pattern}"'
    if path:
        params += f', path: "{path}"'
    ctx.log.write(tool_header("Search", params, ctx), expand=True)
    if ctx.result:
        lines = ctx.result.strip().splitlines()
        ctx.log.write(connector(Text.assemble(
            "Found ", (str(len(lines)), "bold"), " results",
        )), expand=True)


def render_web_fetch(ctx: ToolRenderContext) -> None:
    """Render WebFetch tool call."""
    url = ctx.data.get("url", "")
    ctx.log.write(tool_header("WebFetch", url, ctx), expand=True)


def render_web_search(ctx: ToolRenderContext) -> None:
    """Render WebSearch tool call."""
    query = ctx.data.get("query", "")
    ctx.log.write(tool_header("WebSearch", f'"{query}"', ctx), expand=True)


def render_agent(ctx: ToolRenderContext) -> None:
    """Render Agent tool call."""
    description = ctx.data.get("description", ctx.data.get("prompt", ""))
    if len(description) > 100:
        description = description[:97] + "..."
    ctx.log.write(tool_header("Agent", description, ctx), expand=True)


def render_fallback(ctx: ToolRenderContext) -> None:
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
    ctx.log.write(tool_header(ctx.name, display, ctx), expand=True)


# -- Registry ----------------------------------------------------------------

TOOL_RENDERERS: dict[str, Callable[[ToolRenderContext], None]] = {
    "Bash": render_bash,
    "Read": render_read,
    "Write": render_write,
    "Edit": render_edit,
    "Glob": render_glob,
    "Grep": render_grep,
    "WebFetch": render_web_fetch,
    "WebSearch": render_web_search,
    "Agent": render_agent,
}

HIDDEN_TOOLS = {"TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "ToolSearch"}


# -- Dispatch ----------------------------------------------------------------

def render_tool(log: RichLog, entry, colors: ThemeColors, result=None) -> None:
    """Render a tool use entry in Claude Code style."""
    if entry.name in HIDDEN_TOOLS:
        return

    try:
        data = json.loads(entry.input) if entry.input else {}
    except (json.JSONDecodeError, TypeError):
        data = {}

    ctx = ToolRenderContext(
        log=log,
        name=entry.name,
        data=data,
        colors=colors,
        result=result.content if result else "",
    )

    renderer = TOOL_RENDERERS.get(entry.name, render_fallback)
    renderer(ctx)
