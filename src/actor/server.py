"""MCP server for actor — exposes actor management as tools."""

from __future__ import annotations

import asyncio
import sys
import traceback
from typing import Any, List, Literal

from . import __version__
from . import cli_format

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .errors import ActorError, DaemonUnreachableError
from .service import (
    ActorService,
    Notification,
    RemoteActorService,
    agent_class,
)
from .types import ActorConfig
from .cli import (
    _build_cli_overrides,
    _daemon_socket_uri,
    _resolve_agent_kind_for_cli,
)


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
    # When running from a source clone without install, we can't know
    # the version — skip the drift hint since "unknown" would falsely
    # trigger an `actor update` prompt every session.
    if __version__ == "unknown":
        return base
    # The deployed skill's 'Version and updates' section declares the
    # version it was installed from. The skill itself tells the agent
    # how to compare that against this server announcement.
    return base + f"\n\nactor-sh MCP version: {__version__}."


mcp = ActorMCP("actor.sh", instructions=_build_instructions())


def _resolve_ask_strings() -> dict[str, str]:
    """Resolve the `ask { }` strings from settings.kdl, falling back to
    hardcoded defaults if the kdl is missing/malformed.

    Computed once at module load — tool descriptions are then static
    for the server's lifetime. A malformed settings.kdl logs a warning
    to stderr but doesn't crash the import; defaults take over so MCP
    still works.
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


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------


def _service() -> ActorService:
    """Build a `RemoteActorService` pointing at the standard daemon
    socket. The MCP bridge requires actord to be running — if it
    isn't, calls surface as `DaemonUnreachableError` and the tool
    response carries the message.

    Phase 2 keeps a fresh service per tool call. Subscriptions
    (`_spawn_background_run`) use their own per-spawn service so the
    long-lived subscribe connection doesn't leak across tools.
    """
    return RemoteActorService(_daemon_socket_uri())


# ---------------------------------------------------------------------------
# Channel notification dispatch
# ---------------------------------------------------------------------------


# Registry of in-flight background runs keyed by actor name. The
# value is the session captured at spawn time (the same asyncio loop
# is shared with the publishing service, so we no longer need to
# thread a loop through). Pop on first match so we don't double-emit
# if both `run_completed` and `actor_discarded` arrive (mid-run
# discard).
_active_spawns: dict[str, Any] = {}


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


async def _dispatch_notification(n: Notification) -> None:
    """Service-side notification handler subscribed at server startup.

    Looks up the active spawn for `n.actor`; if there's one, formats
    the event into a `notifications/claude/channel` and dispatches it
    on the running loop. The wire format is byte-identical to the
    pre-refactor implementation: `content = "[<actor>] <body>"`,
    `meta = {"actor": <actor>, "status": <status>}`.

    Plain `actor_discarded` events without an active spawn (e.g. user
    runs `actor discard` with no run in flight) get dropped — no
    parent claude is waiting on them. Mid-run discards arrive with
    `run_id` set and ARE relayed.
    """
    session = _active_spawns.pop(n.actor, None)
    if session is None:
        return

    if n.event == "run_completed":
        status_str = n.status.value if n.status is not None else "unknown"
    elif n.event == "actor_discarded":
        if n.run_id is None:
            # Plain discard — not associated with any spawn we should
            # relay. The pop above already cleaned up.
            return
        status_str = "discarded"
    else:
        return

    body = n.output or f"Finished with status: {status_str}."
    content = f"[{n.actor}] {body}"
    meta = {"actor": n.actor, "status": status_str}

    try:
        await _send_channel_notification(session, content, meta)
    except Exception as e:
        print(
            f"[actor-mcp] failed to send channel notification: {e}",
            file=sys.stderr,
        )


# Subscribe a long-lived service to receive notifications. The service
# itself is constructed per-tool-call (see `_service()`), but
# notifications need a per-spawn subscription that survives across the
# task's lifetime. `_spawn_background_run` builds its own service
# (which it uses for `run_actor`) and subscribes
# `_dispatch_notification` for that one call's lifetime. The handler
# then pops the active spawn and dispatches.
#
# This reads like a pure pub/sub even though every spawn carries its
# own service — the subscription token lives on the per-spawn service
# and gets cancelled in finally so handlers don't leak.


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_actors(status: str | None = None) -> str:
    """List all actors and their status.

    Args:
        status: Optional filter — e.g. "running", "done", "error".
    """
    svc = _service()
    actors = await svc.list_actors(status_filter=status)
    statuses = {a.name: await svc.actor_status(a.name) for a in actors}
    latest_runs = {a.name: await svc.latest_run(a.name) for a in actors}
    return cli_format.format_actor_table(actors, statuses, latest_runs)


@mcp.tool()
async def list_roles() -> str:
    """List available roles from settings.kdl.

    Roles are named presets users define in `~/.actor/settings.kdl`
    (user-wide) or `<repo>/.actor/settings.kdl` (project-local). Each
    role can set the agent, prompt, and any config keys, plus an
    optional `description` that explains when to use it. Apply a role
    at actor creation by passing `role="<name>"` to `new_actor`.
    """
    return cli_format.format_roles(await _service().list_roles())


@mcp.tool()
async def show_actor(name: str, runs: int = 5) -> str:
    """Show full details for an actor including run history.

    Args:
        name: Actor name.
        runs: Number of recent runs to display (default 5, 0 for none).
    """
    detail = await _service().show_actor(name=name, runs_limit=runs)
    return cli_format.format_actor_detail(detail)


@mcp.tool()
async def logs_actor(name: str, verbose: bool = False) -> str:
    """View agent session output for an actor.

    Args:
        name: Actor name.
        verbose: If True, include tool calls, thinking, and timestamps.
    """
    logs = await _service().get_logs(name)
    return cli_format.format_logs(logs, verbose=verbose)


@mcp.tool()
async def stop_actor(name: str) -> str:
    """Stop a running actor.

    Args:
        name: Actor name.
    """
    result = await _service().stop_actor(name=name)
    return cli_format.format_stop(result)


@_ask_tool("on-discard")
async def discard_actor(name: str, force: bool = False) -> str:
    """Remove an actor from the database. Stops it first if running. Worktree stays on disk.

    Args:
        name: Actor name.
        force: If True, ignore on-discard hook failures and discard anyway.
    """
    result = await _service().discard_actor(name=name, force=force)
    return cli_format.format_discard(result)


@mcp.tool()
async def config_actor(name: str, pairs: List[str] | None = None) -> str:
    """View or update actor config.

    Args:
        name: Actor name.
        pairs: Config key=value pairs to set. Omit to view current config.
    """
    svc = _service()
    if pairs:
        await svc.config_actor(name=name, pairs=pairs)
        return f"{name} config updated"
    cfg = await svc.config_actor(name=name)
    return cli_format.format_config_view(cfg)


def _spawn_background_run(
    name: str,
    prompt: str,
    cli_overrides: ActorConfig,
    ctx: Context | None,
) -> None:
    """Schedule a run as a background task on the running loop. The
    service publishes a completion notification; our subscribed
    handler relays it as a `notifications/claude/channel` event."""
    session = ctx.session if ctx else None
    if session is not None:
        _active_spawns[name] = session

    async def _run() -> None:
        # Per-task service so the DB connection isn't shared. SQLite
        # connections opened with `check_same_thread=False` are still
        # cheaper to keep one-per-task than to add cross-task locking.
        svc = _service()
        cancel = await svc.subscribe_notifications(_dispatch_notification)
        try:
            try:
                await svc.run_actor(name=name, prompt=prompt, config=cli_overrides)
            except Exception as e:
                print(
                    f"[actor-mcp] run for '{name}' failed: {e}",
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                # Synthesize an actor_discarded notification for the
                # case where `run_actor` raised because the row was
                # deleted mid-flight (the FK cascade kills the run
                # row; `update_run_status` then raises "run not found"
                # from inside `wait_for_run`). The dispatch handler
                # relays it as status="discarded" when a spawn is
                # registered.
                if not await svc.actor_exists(name):
                    await svc.publish_notification(Notification(
                        actor=name,
                        event="actor_discarded",
                        run_id=-1,  # marker so dispatcher relays
                        output=str(e),
                    ))
                else:
                    # Other failure — still emit a run_completed-ish
                    # event so the orchestrator gets feedback.
                    from .types import Status as _S
                    await svc.publish_notification(Notification(
                        actor=name,
                        event="run_completed",
                        run_id=-1,
                        status=_S.ERROR,
                        output=str(e),
                    ))
        finally:
            cancel()
            # Drop any leftover spawn registration (defensive — the
            # handler usually pops it).
            _active_spawns.pop(name, None)

    asyncio.create_task(_run())


@_ask_tool("on-start")
async def new_actor(
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
    from .config import load_config
    app_config = load_config()
    # MCP mirrors the CLI's positional split: `config` pairs go into
    # agent_args (rejected if they collide with an actor-key); the
    # dedicated `use_subscription` param populates actor_keys
    # directly. Agent resolution mirrors the CLI: explicit `agent`
    # wins, otherwise the role's agent, otherwise "claude".
    agent_kind = _resolve_agent_kind_for_cli(agent, role, app_config)
    agent_cls = agent_class(agent_kind)
    cli_overrides = _build_cli_overrides(
        agent_cls, config or [], use_subscription=use_subscription,
    )

    # Resolve `dir` client-side so the daemon doesn't fall back to its
    # own cwd. When the orchestrator doesn't pass one, default to the
    # MCP server's cwd (the user's working directory wherever
    # `actor main` was launched from).
    if dir is None:
        from pathlib import Path
        dir = str(Path.cwd())
    else:
        from pathlib import Path
        dir = str(Path(dir).expanduser().absolute())

    svc = _service()
    actor = await svc.new_actor(
        name=name,
        dir=dir,
        no_worktree=no_worktree,
        base=base,
        agent_name=agent,
        config=cli_overrides,
        role_name=role,
    )

    if prompt is not None:
        prompt = prompt.strip()
    if prompt:
        try:
            _spawn_background_run(name, prompt, cli_overrides=ActorConfig(), ctx=ctx)
        except Exception as e:
            print(
                f"[actor-mcp] new_actor '{name}' run failed to start: {e}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            return (
                f"Actor '{name}' created at {actor.dir}, "
                f"but run failed to start: {e}"
            )
        return f"Actor '{name}' created at {actor.dir} and is running."
    return f"Actor '{name}' created at {actor.dir}."


@_ask_tool("before-run")
async def run_actor(
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
    actor = await _service().get_actor(name)
    agent_cls = agent_class(actor.agent)
    cli_overrides = _build_cli_overrides(
        agent_cls, config or [], use_subscription=use_subscription,
    )
    _spawn_background_run(name, prompt, cli_overrides=cli_overrides, ctx=ctx)
    return f"Actor '{name}' is running."


async def main(for_host: str | None = None) -> None:
    # for_host is accepted for forward compat but not yet used —
    # FastMCP's instructions are set once at construction (the
    # property is read-only), so host-specific variation will require
    # restructuring when it lands. Warn loudly if the caller passed
    # something we're silently ignoring.
    if for_host is not None and for_host != "claude-code":
        print(
            f"[actor-mcp] note: --for {for_host!r} is accepted but host-specific "
            "behavior isn't wired up yet; running in default mode.",
            file=sys.stderr,
        )
    # FastMCP's sync `mcp.run(transport="stdio")` calls `anyio.run()` which
    # would conflict with the asyncio loop already running in `cli._amain`.
    # Use the async-native entry instead.
    await mcp.run_stdio_async()
