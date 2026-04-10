"""MCP server for actor — exposes actor management as tools."""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any, List

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .db import Database
from .git import RealGit
from .process import RealProcessManager
from .commands import (
    cmd_config,
    cmd_discard,
    cmd_list,
    cmd_logs,
    cmd_new,
    cmd_run,
    cmd_show,
    cmd_stop,
)
from .cli import _db_path, _create_agent


class ActorMCP(FastMCP):
    """FastMCP subclass that declares the claude/channel experimental capability."""

    async def run_stdio_async(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._mcp_server.run(
                read_stream,
                write_stream,
                self._mcp_server.create_initialization_options(
                    experimental_capabilities={"claude/channel": {}},
                ),
            )


mcp = ActorMCP(
    "actor.sh",
    instructions=(
        "Events from the actor channel arrive as <channel source=\"actor\" ...>. "
        "They notify you when an actor finishes. Read the event and report the result to the user."
    ),
)


def _db() -> Database:
    return Database.open(_db_path())


async def _send_channel_notification(
    session: Any,
    content: str,
    meta: dict[str, str] | None = None,
) -> None:
    """Send a notifications/claude/channel event through the session."""
    params: dict[str, Any] = {"content": content}
    if meta:
        params["meta"] = meta
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params=params,
    )
    message = SessionMessage(message=JSONRPCMessage(notification))
    await session.send_message(message)


# -- Tools -----------------------------------------------------------------

@mcp.tool()
def list_actors(status: str | None = None) -> str:
    """List all actors and their status.

    Args:
        status: Optional filter — e.g. "running", "done", "error".
    """
    return cmd_list(_db(), RealProcessManager(), status_filter=status)


@mcp.tool()
def show_actor(name: str, runs: int = 5) -> str:
    """Show full details for an actor including run history.

    Args:
        name: Actor name.
        runs: Number of recent runs to display (default 5, 0 for none).
    """
    return cmd_show(_db(), RealProcessManager(), name=name, runs_limit=runs)


@mcp.tool()
def logs_actor(name: str, verbose: bool = False) -> str:
    """View agent session output for an actor.

    Args:
        name: Actor name.
        verbose: If True, include tool calls, thinking, and timestamps.
    """
    db = _db()
    actor = db.get_actor(name)
    agent = _create_agent(actor.agent)
    return cmd_logs(db, agent, name=name, verbose=verbose, watch=False)


@mcp.tool()
def stop_actor(name: str) -> str:
    """Stop a running actor.

    Args:
        name: Actor name.
    """
    db = _db()
    actor = db.get_actor(name)
    agent = _create_agent(actor.agent)
    return cmd_stop(db, agent, RealProcessManager(), name=name)


@mcp.tool()
def discard_actor(name: str) -> str:
    """Remove an actor from the database. Stops it first if running. Worktree stays on disk.

    Args:
        name: Actor name.
    """
    return cmd_discard(_db(), RealProcessManager(), name=name)


@mcp.tool()
def config_actor(name: str, pairs: List[str] | None = None) -> str:
    """View or update actor config.

    Args:
        name: Actor name.
        pairs: Config key=value pairs to set. Omit to view current config.
    """
    return cmd_config(_db(), name=name, config_pairs=pairs or [])


@mcp.tool()
def run_actor(
    name: str,
    prompt: str,
    create: bool = False,
    agent: str = "claude",
    model: str | None = None,
    dir: str | None = None,
    base: str | None = None,
    no_worktree: bool = False,
    ctx: Context | None = None,
) -> str:
    """Create and/or run an actor with a prompt. Returns immediately — the actor runs in the background.

    Args:
        name: Actor name (becomes the git branch). Use lowercase with hyphens.
        prompt: The task for the actor to work on.
        create: If True, create the actor first (with worktree from current repo).
        agent: Coding agent — "claude" or "codex". Only used with create=True.
        model: Model override (e.g. "opus", "sonnet"). Saved on create, one-off override otherwise.
        dir: Base directory for the worktree. Only used with create=True.
        base: Branch to create the worktree from. Only used with create=True.
        no_worktree: If True, skip worktree creation. Only used with create=True.
    """
    db = _db()
    git = RealGit()
    pm = RealProcessManager()

    config_pairs: list[str] = []
    if model is not None:
        config_pairs.append(f"model={model}")

    if create:
        cmd_new(
            db, git,
            name=name,
            dir=dir,
            no_worktree=no_worktree,
            base=base,
            agent_name=agent,
            config_pairs=config_pairs,
        )

    actor = db.get_actor(name)
    agent_impl = _create_agent(actor.agent)

    # Capture session and event loop for channel notification
    session = ctx.session if ctx else None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def _run() -> None:
        thread_db = Database.open(_db_path())
        output = ""
        try:
            output = cmd_run(
                thread_db, agent_impl, pm,
                name=name,
                prompt=prompt,
                config_pairs=config_pairs if not create else [],
            )
        except Exception as e:
            output = str(e)
            print(f"[actor-mcp] run_actor '{name}' failed: {e}", file=sys.stderr)

        # Read actual status from DB
        resolved = thread_db.resolve_actor_status(name, pm)
        status = resolved.value  # "done", "error", "running", etc.

        # Push channel notification
        if session and loop:
            body = output or f"Finished with status: {status}."
            content = f"[{name}] {body}"
            meta = {"actor": name, "status": status}
            future = asyncio.run_coroutine_threadsafe(
                _send_channel_notification(session, content, meta),
                loop,
            )
            try:
                future.result(timeout=5)
            except Exception as e:
                print(f"[actor-mcp] failed to send channel notification: {e}", file=sys.stderr)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return f"Actor '{name}' is running."


def main() -> None:
    mcp.run(transport="stdio")
