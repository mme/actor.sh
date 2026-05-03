"""MCP server for actor — exposes actor management as tools."""

from __future__ import annotations

import asyncio
import sys
import threading
import traceback
from typing import Any, List, Literal

from . import __version__

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .db import Database
from .errors import ActorError
from .git import RealGit
from .process import RealProcessManager
from .types import ActorConfig
from .commands import (
    _agent_class,
    cmd_config,
    cmd_discard,
    cmd_list,
    cmd_logs,
    cmd_new,
    cmd_roles,
    cmd_run,
    cmd_show,
    cmd_stop,
)
from .cli import _build_cli_overrides, _db_path, _create_agent, _resolve_agent_kind_for_cli


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


def _build_instructions(for_host: str | None = None) -> str:
    base = (
        "Events from the actor channel arrive as <channel source=\"actor\" ...>. "
        "They notify you when an actor finishes. Read the event and report the result to the user."
    )
    # When running from a source clone without install, we can't know the
    # version — skip the drift hint since "unknown" would falsely trigger an
    # `actor update` prompt every session.
    if __version__ == "unknown":
        return base
    # The deployed skill's 'Version and updates' section (inside the
    # <!-- BEGIN AUTO-UPDATED ... --> markers of SKILL.md) declares the
    # version it was installed from. The skill itself tells the agent how
    # to compare that against this server announcement.
    return (
        base
        + f"\n\nactor-sh MCP version: {__version__}."
    )


mcp = ActorMCP("actor.sh", instructions=_build_instructions())


def _resolve_ask_strings() -> dict[str, str]:
    """Resolve the `ask { }` strings from settings.kdl, falling back to
    hardcoded defaults if the kdl is missing/malformed.

    Computed once at module load — tool descriptions are then static for
    the server's lifetime. A malformed settings.kdl logs a warning to
    stderr but doesn't crash the import; defaults take over so MCP still
    works.
    """
    from .config import ASK_DEFAULTS, load_config
    try:
        cfg = load_config()
    except Exception as e:
        print(
            f"[actor-mcp] ignoring settings.kdl for ask block (will use "
            f"hardcoded defaults): {e}",
            file=sys.stderr,
        )
        return dict(ASK_DEFAULTS)
    return {key: cfg.ask.resolved(key, default) for key, default in ASK_DEFAULTS.items()}


_ASK_RESOLVED: dict[str, str] = _resolve_ask_strings()


def _ask_tool(ask_key: str):
    """Decorator wrapper around `@mcp.tool()` that appends the resolved
    `ask` guidance for `ask_key` to the tool's docstring before
    registering. Empty / silenced ask values append nothing — the
    docstring stays as-is."""
    def decorator(func):
        base = (func.__doc__ or "").rstrip()
        appendix = _ASK_RESOLVED.get(ask_key, "")
        description = base + ("\n\n" + appendix if appendix else "")
        return mcp.tool(description=description)(func)
    return decorator


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
def list_roles() -> str:
    """List available roles from settings.kdl.

    Roles are named presets users define in `~/.actor/settings.kdl` (user-wide)
    or `<repo>/.actor/settings.kdl` (project-local). Each role can set the
    agent, prompt, and any config keys, plus an optional `description` that
    explains when to use it. Apply a role at actor creation by passing
    `role="<name>"` to `new_actor`.
    """
    from .config import load_config
    return cmd_roles(load_config())


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


@_ask_tool("on-discard")
def discard_actor(name: str, force: bool = False) -> str:
    """Remove an actor from the database. Stops it first if running. Worktree stays on disk.

    Args:
        name: Actor name.
        force: If True, ignore on-discard hook failures and discard anyway.
    """
    from .config import load_config
    return cmd_discard(
        _db(), RealProcessManager(), name=name,
        app_config=load_config(), force=force,
    )


@mcp.tool()
def config_actor(name: str, pairs: List[str] | None = None) -> str:
    """View or update actor config.

    Args:
        name: Actor name.
        pairs: Config key=value pairs to set. Omit to view current config.
    """
    return cmd_config(_db(), name=name, config_pairs=pairs or [])


def _spawn_background_run(
    name: str,
    prompt: str,
    cli_overrides: ActorConfig,
    ctx: Context | None,
) -> None:
    """Kick off a run in a background thread; send a channel notification when it finishes."""
    pm = RealProcessManager()
    actor = _db().get_actor(name)
    agent_impl = _create_agent(actor.agent)

    session = ctx.session if ctx else None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def _run() -> None:
        thread_db = Database.open(_db_path())
        output = ""
        try:
            from .config import load_config as _lc
            output = cmd_run(
                thread_db, agent_impl, pm,
                name=name,
                prompt=prompt,
                cli_overrides=cli_overrides,
                app_config=_lc(),
            )
        except Exception as e:
            output = str(e)
            print(f"[actor-mcp] run for '{name}' failed: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        # If the actor row is gone, the run was terminated by a
        # `discard` (not just a `stop`). Distinguish in the channel
        # notification meta so the parent Claude can react
        # accordingly — "discarded" implies the actor + worktree are
        # gone and can't be re-run, vs "stopped" which leaves the
        # actor in place. We can't add `Status.DISCARDED` to the enum
        # because the row doesn't exist to carry it; the literal
        # string in the meta dict is the canonical signal.
        if thread_db.actor_exists(name):
            resolved = thread_db.resolve_actor_status(name, pm)
            status = resolved.value
        else:
            status = "discarded"

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

    threading.Thread(target=_run, daemon=True).start()


@_ask_tool("on-start")
def new_actor(
    name: str,
    prompt: str | None = None,
    agent: Literal["claude", "codex"] | None = None,
    role: str | None = None,
    dir: str | None = None,
    base: str | None = None,
    no_worktree: bool = False,
    config: List[str] | None = None,
    use_subscription: bool | None = None,
    ctx: Context | None = None,
) -> str:
    """Create a new actor. If a prompt is given, also runs it in the background.

    Args:
        name: Actor name (becomes the git branch). Use lowercase with hyphens.
        prompt: Optional task prompt to run immediately after creation. The
            role's `prompt` field is its system prompt (injected as
            `--append-system-prompt`), not a task fallback — omit `prompt`
            to create the actor idle and run it later with `run_actor`.
        agent: Coding agent — "claude" or "codex". Defaults to the role's
            agent (if a role is applied) or "claude" otherwise.
        role: Apply a named role from settings.kdl. Use `list_roles` to see
            what's defined. The role's agent + config snapshot onto the
            actor; an unknown name fails with the available list.
        dir: Base directory (repo root) for the worktree. **Must be an
            absolute path** when set — relative paths resolve against the
            MCP server's cwd, which is rarely what the caller intends and
            is fragile across sessions. Defaults to the orchestrator
            session's cwd (i.e. wherever the user ran `actor main`), which
            is the right default when sub-actors should work in the same
            repo. Pass an explicit absolute path when targeting a
            different repo.
        base: Branch to create the worktree from (defaults to current branch).
        no_worktree: If True, skip worktree creation.
        config: Config key=value pairs saved as actor defaults (e.g. ["model=opus", "effort=max"]).
            Agent-args only; actor-keys like "use-subscription" are rejected here — use the
            dedicated parameter below. CLI-level overrides beat the role's values.
        use_subscription: When True, force subscription auth (strip the agent's API key env var).
            When False, pass the API key through. When omitted (None), defer to lower-precedence
            layers (role / settings.kdl / class default).
    """
    db = _db()
    git = RealGit()
    from .config import load_config
    app_config = load_config()
    # MCP mirrors the CLI's positional split: `config` pairs go into
    # agent_args (rejected if they collide with an actor-key); the dedicated
    # `use_subscription` param populates actor_keys directly. Validation
    # and bucketing are the same code path as the CLI. Agent resolution also
    # mirrors the CLI: explicit `agent` wins, otherwise the role's agent,
    # otherwise "claude".
    agent_kind = _resolve_agent_kind_for_cli(agent, role, app_config)
    agent_cls = _agent_class(agent_kind)
    cli_overrides = _build_cli_overrides(
        agent_cls, config or [], use_subscription=use_subscription,
    )
    actor = cmd_new(
        db, git,
        name=name,
        dir=dir,
        no_worktree=no_worktree,
        base=base,
        agent_name=agent,
        cli_overrides=cli_overrides,
        role_name=role,
        app_config=app_config,
    )

    if prompt is not None:
        prompt = prompt.strip()
    if prompt:
        try:
            _spawn_background_run(name, prompt, cli_overrides=ActorConfig(), ctx=ctx)
        except Exception as e:
            print(f"[actor-mcp] new_actor '{name}' run failed to start: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return f"Actor '{name}' created at {actor.dir}, but run failed to start: {e}"
        return f"Actor '{name}' created at {actor.dir} and is running."
    return f"Actor '{name}' created at {actor.dir}."


@_ask_tool("before-run")
def run_actor(
    name: str,
    prompt: str,
    config: List[str] | None = None,
    use_subscription: bool | None = None,
    ctx: Context | None = None,
) -> str:
    """Run an existing actor with a prompt. Returns immediately — the actor runs in the background.

    Args:
        name: Actor name.
        prompt: The task for the actor to work on.
        config: Per-run config overrides (e.g. ["model=opus"]). Not saved to actor defaults — use config_actor
            to change defaults. Agent-args only; actor-keys like "use-subscription" are rejected here —
            use the dedicated parameter below.
        use_subscription: Per-run actor-key override. When True, force subscription auth for this run
            (strip the agent's API key env var). When False, pass the API key through. When omitted
            (None), use the actor's stored value.
    """
    prompt = prompt.strip()
    if not prompt:
        raise ActorError("prompt is required")
    # MCP mirrors the CLI's positional split: `config` pairs go into
    # agent_args (rejected if they collide with an actor-key); the dedicated
    # `use_subscription` param populates actor_keys directly.
    actor = _db().get_actor(name)
    agent_cls = _agent_class(actor.agent)
    cli_overrides = _build_cli_overrides(
        agent_cls, config or [], use_subscription=use_subscription,
    )
    _spawn_background_run(name, prompt, cli_overrides=cli_overrides, ctx=ctx)
    return f"Actor '{name}' is running."


def main(for_host: str | None = None) -> None:
    # for_host is accepted for forward compat but not yet used — FastMCP's
    # instructions are set once at construction (the property is read-only),
    # so host-specific variation will require restructuring when it lands.
    # Warn loudly if the caller passed something we're silently ignoring.
    if for_host is not None and for_host != "claude-code":
        print(
            f"[actor-mcp] note: --for {for_host!r} is accepted but host-specific "
            "behavior isn't wired up yet; running in default mode.",
            file=sys.stderr,
        )
    mcp.run(transport="stdio")
