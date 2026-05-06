"""ActorService: the single boundary between commands and storage.

Every state-mutating operation on actors / runs flows through this
interface. CLI, MCP server, and watch's interactive manager call
service methods rather than touching `Database` / `Agent` / `GitOps`
directly. A future `RemoteActorService` will dispatch over the wire to
`actord` (issue #35); swapping it in is a one-line change at each
construction site, with no command-layer rewrites required.

Service methods return structured types — `RunResult`, `ActorDetail`,
… — never pre-formatted strings. Display formatting lives in
`actor.cli_format` so non-CLI callers (MCP, future remote) can reuse
the data without re-parsing.

Notifications are an in-process pub/sub. Sync handlers run inline in
the publishing task; coroutine handlers are scheduled as tasks on the
running loop. The MCP-side handler is sync; future async handlers can
register straight away.

Port 2204 is reserved for actord (issue #35) and intentionally not
bound here.
"""
from __future__ import annotations

import abc
import asyncio
import contextvars
import inspect
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Literal, Optional, Tuple, Union

from .agents.claude import ClaudeAgent
from .agents.codex import CodexAgent
from .config import AppConfig, Hooks, Role
from .db import Database
from .errors import (
    ActorError,
    AgentNotFoundError,
    ConfigError,
    DaemonUnreachableError,
    HookFailedError,
    IsRunningError,
    NotRunningError,
)
from .hooks import HookRunner, hook_env, run_hook
from .interfaces import Agent, GitOps, LogEntry, ProcessManager, binary_exists
from .types import (
    Actor,
    ActorConfig,
    AgentKind,
    Run,
    Status,
    _now_iso,
    _parse_iso,
    _sorted_config,
    validate_name,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTERACTIVE_PROMPT = "*interactive*"


# Per-call AppConfig override. The daemon-side dispatcher loads
# settings.kdl from the *caller's* cwd (so project settings under
# `<cwd>/.actor/settings.kdl` are visible) and sets this contextvar
# for the duration of the RPC handler. `LocalActorService` reads it
# via `_resolve_app_config` and falls back to the constructor-supplied
# `app_config` if the var is unset (e.g. local CLI usage).
#
# `ContextVar` gives us per-Task isolation, so concurrent RPC clients
# with different cwds don't trample each other's config — each
# websocket connection runs in its own asyncio Task.
_app_config_override: "contextvars.ContextVar[Optional[AppConfig]]" = (
    contextvars.ContextVar("_app_config_override", default=None)
)

# Per-call parent-actor override. Pre-Phase-2 the parent was read from
# `os.environ["ACTOR_NAME"]`, which still works for in-process callers
# (the local interactive path) but breaks for the daemon — its env is
# the daemon process's, not the CLI's. The remote dispatcher binds
# this contextvar from `_caller_actor_name`; LocalActorService reads
# the contextvar first, then falls back to os.environ.
_caller_actor_name: "contextvars.ContextVar[Optional[str]]" = (
    contextvars.ContextVar("_caller_actor_name", default=None)
)

_DEFAULT_ON_DISCARD = "git diff --quiet"
"""Default on-discard hook command. Fires only when an `app_config`
was wired into the service AND the user didn't set `on-discard` in
settings.kdl. Intent: don't let the user accidentally throw away
uncommitted work — `git diff --quiet` exits 0 only when the worktree
is clean. To suppress without configuring a real check, set
`on-discard "true"` in settings.kdl."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStartResult:
    run_id: int
    pid: Optional[int]
    status: Status


@dataclass(frozen=True)
class RunResult:
    run_id: int
    actor: str
    status: Status
    exit_code: Optional[int]
    output: str


@dataclass(frozen=True)
class StopResult:
    name: str
    was_alive: bool


@dataclass(frozen=True)
class DiscardResult:
    names: List[str]


@dataclass(frozen=True)
class ActorDetail:
    actor: Actor
    status: Status
    runs: List[Run]
    total_runs: int
    # Carries the caller's `runs_limit`. 0 means "don't render the
    # runs section at all" (vs 5 with `runs == []` meaning "no runs
    # yet"). Lives here rather than on the formatter so non-CLI
    # consumers (MCP, future remote) get the same semantics.
    runs_limit: int = 5


@dataclass(frozen=True)
class InteractiveRunHandle:
    run_id: int
    session_id: str
    dir: Path
    argv: List[str]


@dataclass(frozen=True)
class LogsResult:
    session_id: Optional[str]
    entries: List[LogEntry]


@dataclass(frozen=True)
class Notification:
    """In-process event published by the service.

    `event="run_completed"` fires when `wait_for_run` /
    `finalize_interactive_run` writes the terminal status. `status`
    carries the run's final `Status` enum value.

    `event="actor_discarded"` fires from `discard_actor` after the
    row is gone, OR from `wait_for_run` when the actor row vanished
    mid-wait (mid-run discard). In the mid-run case `run_id` is set
    and `output` carries whatever the agent wrote before being
    killed; for plain discards both are None.
    """
    actor: str
    event: Literal["run_completed", "actor_discarded"]
    run_id: Optional[int] = None
    status: Optional[Status] = None
    output: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Agent class lookup (callable utility — used by CLI validators too)
# ---------------------------------------------------------------------------


_AGENT_CLASS_BY_KIND = {
    AgentKind.CLAUDE: ClaudeAgent,
    AgentKind.CODEX: CodexAgent,
}


def agent_class(kind: AgentKind):
    """Map an AgentKind to its Agent subclass — for reading
    class-level constants (`ACTOR_DEFAULTS`, `AGENT_DEFAULTS`,
    `SYSTEM_PROMPT_KEY`) without instantiating an agent."""
    try:
        return _AGENT_CLASS_BY_KIND[kind]
    except KeyError:
        raise ActorError(f"unknown agent kind: {kind}")


def create_agent(kind: AgentKind) -> Agent:
    """Default agent factory: instantiate the agent class for `kind`."""
    return agent_class(kind)()


# ---------------------------------------------------------------------------
# Notification handler types
# ---------------------------------------------------------------------------


Cancel = Callable[[], None]
NotificationHandler = Callable[[Notification], Union[None, Awaitable[None]]]


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class ActorService(abc.ABC):
    """Single boundary between commands and storage."""

    # -- Lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    async def new_actor(
        self,
        name: str,
        dir: Optional[str],
        no_worktree: bool,
        base: Optional[str],
        agent_name: Optional[str],
        config: ActorConfig,
        role_name: Optional[str] = None,
    ) -> Actor: ...

    @abc.abstractmethod
    async def discard_actor(self, name: str, force: bool = False) -> DiscardResult: ...

    @abc.abstractmethod
    async def config_actor(
        self, name: str, pairs: Optional[List[str]] = None,
    ) -> ActorConfig: ...

    # -- Runs --------------------------------------------------------------

    @abc.abstractmethod
    async def start_run(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunStartResult: ...

    @abc.abstractmethod
    async def wait_for_run(self, run_id: int) -> RunResult: ...

    @abc.abstractmethod
    async def run_actor(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunResult: ...

    @abc.abstractmethod
    async def stop_actor(self, name: str) -> StopResult: ...

    # -- Discovery ---------------------------------------------------------

    @abc.abstractmethod
    async def get_actor(self, name: str) -> Actor: ...

    @abc.abstractmethod
    async def actor_exists(self, name: str) -> bool: ...

    @abc.abstractmethod
    async def list_actors(self, status_filter: Optional[str] = None) -> List[Actor]: ...

    @abc.abstractmethod
    async def actor_status(self, name: str) -> Status: ...

    @abc.abstractmethod
    async def latest_run(self, actor_name: str) -> Optional[Run]: ...

    @abc.abstractmethod
    async def show_actor(self, name: str, runs_limit: int = 5) -> ActorDetail: ...

    @abc.abstractmethod
    async def list_runs(self, actor_name: str, limit: int) -> Tuple[List[Run], int]: ...

    @abc.abstractmethod
    async def get_run(self, run_id: int) -> Optional[Run]: ...

    @abc.abstractmethod
    async def get_logs(self, actor_name: str) -> LogsResult: ...

    @abc.abstractmethod
    async def list_roles(self) -> Dict[str, Role]: ...

    # -- Notifications -----------------------------------------------------

    @abc.abstractmethod
    async def publish_notification(self, n: Notification) -> None: ...

    @abc.abstractmethod
    async def subscribe_notifications(
        self, handler: NotificationHandler,
    ) -> Cancel: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _home_dir() -> Path:
    home = os.environ.get("HOME", "")
    if not home:
        raise ActorError("HOME environment variable is not set")
    return Path(home)


def _worktree_path(name: str) -> Path:
    return _home_dir() / ".actor" / "worktrees" / name


def _default_interactive_runner(argv: List[str], cwd: Path, env: dict) -> int:
    import subprocess
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                return proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Give up rather than block the shell forever. Surface a
                # warning so the user can chase a zombie/stuck process.
                print(
                    f"warning: child pid {proc.pid} did not exit after SIGKILL",
                    file=sys.stderr,
                )
                return -1


# ---------------------------------------------------------------------------
# LocalActorService
# ---------------------------------------------------------------------------


AgentFactory = Callable[[AgentKind], Agent]


class LocalActorService(ActorService):
    """In-process service. Owns one `Database`, one `GitOps`, one
    `ProcessManager`, an agent factory, and an in-process notification
    pub/sub. The `AppConfig` is loaded once at process startup; the
    service does NOT re-read settings.kdl."""

    def __init__(
        self,
        db: Database,
        git: GitOps,
        proc_mgr: ProcessManager,
        agent_factory: AgentFactory = create_agent,
        app_config: Optional[AppConfig] = None,
        hook_runner: Optional[HookRunner] = None,
    ) -> None:
        self._db = db
        self._git = git
        self._proc_mgr = proc_mgr
        self._agent_factory = agent_factory
        self._app_config = app_config
        self._hook_runner = hook_runner
        self._handlers: List[NotificationHandler] = []
        # Agents carry per-instance state — `ClaudeAgent._children`
        # tracks subprocess handles between `start()` and `wait()`.
        # Splitting the run lifecycle into `start_run` / `wait_for_run`
        # means both must see the same agent instance, so we cache one
        # per kind for the lifetime of the service.
        self._agent_cache: Dict[AgentKind, Agent] = {}

    # -- Internal helpers --------------------------------------------------

    def _resolved_config(self) -> Optional[AppConfig]:
        override = _app_config_override.get()
        return override if override is not None else self._app_config

    def _hooks(self) -> Hooks:
        cfg = self._resolved_config()
        return cfg.hooks if cfg is not None else Hooks()

    def _roles_dict(self) -> Dict[str, Role]:
        cfg = self._resolved_config()
        return dict(cfg.roles) if cfg is not None else {}

    def _agent(self, kind: AgentKind) -> Agent:
        inst = self._agent_cache.get(kind)
        if inst is None:
            inst = self._agent_factory(kind)
            self._agent_cache[kind] = inst
        return inst

    async def _db_call(self, fn, /, *args, **kwargs):
        """Run a sync `Database` method on a worker thread.

        SQLite is sync; the service is async. `to_thread` is the
        narrowest bridge — it preserves the existing connection
        (`check_same_thread=False`) without pulling in a separate
        async-sqlite dependency."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- Lifecycle ---------------------------------------------------------

    async def new_actor(
        self,
        name: str,
        dir: Optional[str],
        no_worktree: bool,
        base: Optional[str],
        agent_name: Optional[str],
        config: ActorConfig,
        role_name: Optional[str] = None,
    ) -> Actor:
        validate_name(name)

        role = None
        if role_name is not None:
            roles = self._roles_dict()
            if role_name not in roles:
                available = sorted(roles)
                hint = (
                    f"available: {', '.join(available)}"
                    if available
                    else "no roles defined in settings.kdl"
                )
                raise ConfigError(f"unknown role: '{role_name}' ({hint})")
            role = roles[role_name]

        # Agent precedence: explicit CLI flag > role's agent > "claude"
        if agent_name is None:
            agent_name = role.agent if (role and role.agent) else "claude"
        agent_kind = AgentKind.from_str(agent_name)

        if not binary_exists(agent_kind.binary_name):
            print(
                f"warning: '{agent_kind.binary_name}' not found on PATH",
                file=sys.stderr,
            )

        # Config precedence (lowest → highest), merged into two
        # side-by-side dicts (actor_keys, agent_args) — the split is
        # preserved positionally across every layer; nothing
        # downstream reconstructs it via name lookup:
        #   1. Agent class defaults (ACTOR_DEFAULTS / AGENT_DEFAULTS)
        #   2. kdl `defaults "<name>" { ... }` for this agent_kind
        #   3. Role config (kdl role is a flat namespace; we partition
        #      each key here using the agent class's ACTOR_DEFAULTS)
        #   4. CLI overrides (already structured by the caller)
        # Only layer 2 can carry `None` (kdl's `null` cancel marker).
        agent_cls = agent_class(agent_kind)
        merged_actor_keys: Dict[str, Optional[str]] = dict(agent_cls.ACTOR_DEFAULTS)
        merged_agent_args: Dict[str, Optional[str]] = dict(agent_cls.AGENT_DEFAULTS)

        cfg = self._resolved_config()
        if cfg is not None:
            kdl_defaults = cfg.agent_defaults.get(agent_kind.value)
            if kdl_defaults is not None:
                for k, v in kdl_defaults.actor_keys.items():
                    if v is None:
                        merged_actor_keys.pop(k, None)
                    else:
                        merged_actor_keys[k] = v
                for k, v in kdl_defaults.agent_args.items():
                    if v is None:
                        merged_agent_args.pop(k, None)
                    else:
                        merged_agent_args[k] = v

        if role is not None:
            for k, v in role.config.items():
                if k in agent_cls.ACTOR_DEFAULTS:
                    merged_actor_keys[k] = v
                else:
                    merged_agent_args[k] = v
            if role.prompt:
                sp_key = agent_cls.SYSTEM_PROMPT_KEY
                if sp_key is None:
                    raise ConfigError(
                        f"role '{role.name}' has a `prompt` field, but agent "
                        f"'{agent_kind.value}' doesn't yet support role-level "
                        f"system prompts. Either remove the prompt and put the "
                        f"guidance in the per-call task prompt, or use a "
                        f"claude-based role."
                    )
                merged_agent_args.setdefault(sp_key, role.prompt)

        for k, v in config.actor_keys.items():
            merged_actor_keys[k] = v
        for k, v in config.agent_args.items():
            merged_agent_args[k] = v

        resolved_config = ActorConfig(
            actor_keys=_sorted_config(
                {k: v for k, v in merged_actor_keys.items() if v is not None}
            ),
            agent_args=_sorted_config(
                {k: v for k, v in merged_agent_args.items() if v is not None}
            ),
        )

        if dir is not None:
            try:
                base_dir = Path(dir).resolve(strict=True)
            except (OSError, ValueError) as e:
                # `Path.resolve(strict=True)` reports the deepest
                # existing ancestor in OSError, not the original path.
                # Include the caller's input verbatim so the message
                # points at what they typed.
                raise ActorError(f"cannot resolve --dir {dir!r}: {e}")
            if not base_dir.is_dir():
                raise ActorError(f"--dir {dir!r} is not a directory")
        else:
            base_dir = Path.cwd()

        use_worktree = (not no_worktree) and await self._git.is_repo(base_dir)

        if not use_worktree:
            actor_dir = str(base_dir)
            source_repo: Optional[str] = None
            base_branch: Optional[str] = None
            worktree = False
        else:
            branch_base = (
                base if base is not None
                else await self._git.current_branch(base_dir)
            )
            wt_path = _worktree_path(name)
            await self._git.create_worktree(base_dir, wt_path, name, branch_base)
            actor_dir = str(wt_path)
            source_repo = str(base_dir)
            base_branch = branch_base
            worktree = True

        parent = _caller_actor_name.get() or os.environ.get("ACTOR_NAME")

        now = _now_iso()
        actor = Actor(
            name=name,
            agent=agent_kind,
            agent_session=None,
            dir=actor_dir,
            source_repo=source_repo,
            base_branch=base_branch,
            worktree=worktree,
            parent=parent,
            config=resolved_config,
            created_at=now,
            updated_at=now,
        )

        try:
            await self._db_call(self._db.insert_actor, actor)
        except ActorError:
            if worktree:
                wt_path = _worktree_path(name)
                try:
                    await self._git.remove_worktree(Path(source_repo), wt_path)  # type: ignore[arg-type]
                except Exception as cleanup_err:
                    print(
                        f"warning: failed to clean up worktree at {wt_path}: {cleanup_err}",
                        file=sys.stderr,
                    )
            raise

        # on-start hook fires after the actor row + worktree exist so
        # the hook script can assume both. Non-zero exit rolls
        # everything back.
        on_start = self._hooks().on_start
        if on_start is not None:
            env = hook_env(
                os.environ,
                actor_name=name,
                actor_dir=Path(actor_dir),
                actor_agent=agent_kind.value,
                actor_session_id=None,
            )
            try:
                await run_hook(
                    "on-start", on_start, env, Path(actor_dir),
                    runner=self._hook_runner,
                )
            except Exception:
                try:
                    await self._db_call(self._db.delete_actor, name)
                except Exception as rollback_err:
                    print(
                        f"warning: failed to roll back actor row for '{name}' "
                        f"after on-start hook failure: {rollback_err}",
                        file=sys.stderr,
                    )
                if worktree:
                    assert source_repo is not None
                    wt_path = _worktree_path(name)
                    try:
                        await self._git.remove_worktree(Path(source_repo), wt_path)
                    except Exception as cleanup_err:
                        print(
                            f"warning: failed to clean up worktree at {wt_path}: {cleanup_err}",
                            file=sys.stderr,
                        )
                raise

        return actor

    async def discard_actor(self, name: str, force: bool = False) -> DiscardResult:
        """Discard an actor — stops it if running, runs the on-discard
        hook, removes the worktree, deletes the DB row.

        Children (recursively discovered via the `parent` column) are
        processed depth-first — leaves first — so a parent is only
        deleted once all its descendants are gone. If any discard in
        the chain raises, the chain stops and the surfaced exception
        names the actor that broke the chain.
        """
        result = DiscardResult(names=[])
        await self._discard_recursive(name, force=force, visited=set(), result=result)
        return result

    async def _discard_recursive(
        self,
        name: str,
        *,
        force: bool,
        visited: set,
        result: DiscardResult,
    ) -> None:
        actor = await self._db_call(self._db.get_actor, name)
        visited.add(name)

        children = await self._db_call(self._db.list_children, name)
        for child in children:
            if child.name not in visited:
                await self._discard_recursive(
                    child.name, force=force, visited=visited, result=result,
                )

        # Stop if running. Has to happen BEFORE the hook so the hook
        # runs against settled working-tree state.
        status = await self._db_call(
            self._db.resolve_actor_status, name, self._proc_mgr,
        )
        if status == Status.RUNNING:
            await self._force_stop(name)

        # Default `git diff --quiet` ONLY when an `app_config` was
        # wired in — service constructed without a config (some tests)
        # gets no default hook so a stray `git diff` doesn't bite.
        if self._resolved_config() is not None:
            on_discard = self._hooks().on_discard or _DEFAULT_ON_DISCARD
        else:
            on_discard = None

        if on_discard is not None:
            actor_dir = Path(actor.dir)
            hook_cwd = actor_dir if actor_dir.is_dir() else Path.home()
            env = hook_env(
                os.environ,
                actor_name=name,
                actor_dir=actor_dir,
                actor_agent=actor.agent.value,
                actor_session_id=actor.agent_session,
            )
            try:
                await run_hook(
                    "on-discard", on_discard, env, hook_cwd,
                    runner=self._hook_runner,
                )
            except HookFailedError as e:
                if force:
                    print(
                        f"warning: on-discard hook failed for '{name}' but "
                        f"--force was set; discarding anyway: {e}",
                        file=sys.stderr,
                    )
                else:
                    raise HookFailedError(
                        e.event,
                        e.command,
                        e.exit_code,
                        stdout=e.stdout,
                        stderr=(
                            f"actor '{name}' discard aborted; "
                            f"{e.stderr}" if e.stderr
                            else f"actor '{name}' discard aborted"
                        ),
                    ) from e

        # Remove the worktree if the actor was created with one.
        # Failure here aborts the discard (no DB delete) unless force
        # — otherwise we'd leave a dangling worktree on disk.
        #
        # Intentionally NOT deleting the underlying git branch: the
        # default on-discard hook is `git diff --quiet`, which only
        # checks unstaged modifications — committed work would be
        # silently destroyed. Trade-off: `actor new <same>` after
        # discard fails with "branch already exists"; recovery is
        # `git branch -D <name>` in the source repo (or rename).
        if actor.worktree and actor.source_repo:
            wt_path = Path(actor.dir)
            if wt_path.is_dir():
                try:
                    await self._git.remove_worktree(Path(actor.source_repo), wt_path)
                except Exception as e:
                    if force:
                        print(
                            f"warning: failed to remove worktree {wt_path} for "
                            f"'{name}'; --force is set so DB delete proceeds: {e}",
                            file=sys.stderr,
                        )
                    else:
                        raise ActorError(
                            f"failed to remove worktree {wt_path} for actor "
                            f"'{name}': {e}"
                        ) from e

        await self._db_call(self._db.delete_actor, name)
        result.names.append(name)
        await self.publish_notification(Notification(
            actor=name, event="actor_discarded",
        ))

    async def config_actor(
        self, name: str, pairs: Optional[List[str]] = None,
    ) -> ActorConfig:
        actor = await self._db_call(self._db.get_actor, name)

        if not pairs:
            return actor.config

        # `key=` (explicit empty value) deletes the key from the
        # stored config rather than persisting an empty string. Bare
        # `key` (no `=`) means "boolean flag" and stays as "" — the
        # convention used by `parse_config`.
        pairs_with_intent: list[tuple[str, str, bool]] = []
        for pair in pairs:
            if "=" in pair:
                k, v = pair.split("=", 1)
                pairs_with_intent.append((k, v, v == ""))
            else:
                pairs_with_intent.append((pair, "", False))

        agent_cls = agent_class(actor.agent)
        new_actor_keys = dict(actor.config.actor_keys)
        new_agent_args = dict(actor.config.agent_args)
        for k, v, delete in pairs_with_intent:
            target = (
                new_actor_keys if k in agent_cls.ACTOR_DEFAULTS
                else new_agent_args
            )
            if delete:
                target.pop(k, None)
            else:
                target[k] = v

        new_config = ActorConfig(
            actor_keys=_sorted_config(new_actor_keys),
            agent_args=_sorted_config(new_agent_args),
        )
        await self._db_call(self._db.update_actor_config, name, new_config)
        return new_config

    # -- Run lifecycle -----------------------------------------------------

    async def start_run(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunStartResult:
        actor = await self._db_call(self._db.get_actor, name)

        status = await self._db_call(
            self._db.resolve_actor_status, name, self._proc_mgr,
        )
        if status == Status.RUNNING:
            raise IsRunningError(name)

        if not binary_exists(actor.agent.binary_name):
            raise AgentNotFoundError(actor.agent.binary_name)

        dir_path = Path(actor.dir)
        if not dir_path.is_dir():
            raise ActorError(
                f"actor directory '{actor.dir}' does not exist — "
                f"use 'actor discard {name}' to clean up"
            )

        # before-run hook fires before the Run row is inserted so a
        # failing pre-flight check doesn't leave a phantom run.
        before_run = self._hooks().before_run
        if before_run is not None:
            env = hook_env(
                os.environ,
                actor_name=name,
                actor_dir=dir_path,
                actor_agent=actor.agent.value,
                actor_session_id=actor.agent_session,
            )
            await run_hook(
                "before-run", before_run, env, dir_path,
                runner=self._hook_runner,
            )

        # Merge config: actor defaults + run overrides. The split is
        # preserved positionally — actor_keys and agent_args layer
        # independently.
        merged_actor_keys = dict(actor.config.actor_keys)
        merged_actor_keys.update(config.actor_keys)
        merged_agent_args = dict(actor.config.agent_args)
        merged_agent_args.update(config.agent_args)
        effective_config = ActorConfig(
            actor_keys=_sorted_config(merged_actor_keys),
            agent_args=_sorted_config(merged_agent_args),
        )

        # Insert RUNNING row BEFORE starting the agent so list/show
        # see it immediately.
        run = Run(
            id=0,
            actor_name=name,
            prompt=prompt,
            status=Status.RUNNING,
            exit_code=None,
            pid=None,
            config=effective_config,
            started_at=_now_iso(),
            finished_at=None,
        )
        run_id = await self._db_call(self._db.insert_run, run)
        await self._db_call(self._db.touch_actor, name)

        # Expose actor name to the agent process (set for child,
        # restored after).
        prev_actor_name = os.environ.get("ACTOR_NAME")
        os.environ["ACTOR_NAME"] = name

        agent_inst = self._agent(actor.agent)
        try:
            if actor.agent_session is not None:
                pid = await agent_inst.resume(
                    dir_path, actor.agent_session, prompt, effective_config,
                )
                new_session: Optional[str] = None
            else:
                pid, new_session = await agent_inst.start(
                    dir_path, prompt, effective_config,
                )
        except Exception:
            await self._db_call(
                self._db.update_run_status, run_id, Status.ERROR, -1,
            )
            raise
        finally:
            if prev_actor_name is None:
                os.environ.pop("ACTOR_NAME", None)
            else:
                os.environ["ACTOR_NAME"] = prev_actor_name

        await self._db_call(self._db.update_run_pid, run_id, pid)
        if new_session is not None:
            await self._db_call(self._db.update_actor_session, name, new_session)

        return RunStartResult(run_id=run_id, pid=pid, status=Status.RUNNING)

    async def wait_for_run(self, run_id: int) -> RunResult:
        run_row = await self._db_call(self._db.get_run, run_id)
        if run_row is None:
            raise ActorError(f"run {run_id} not found")
        actor_name = run_row.actor_name
        actor = await self._db_call(self._db.get_actor, actor_name)
        agent_inst = self._agent(actor.agent)

        pid = run_row.pid
        if pid is None:
            # `start_run` writes the pid before returning — landing
            # here means start_run failed in a way that didn't raise.
            try:
                await self._db_call(
                    self._db.update_run_status, run_id, Status.ERROR, -1,
                )
            except ActorError:
                pass
            return RunResult(
                run_id=run_id, actor=actor_name,
                status=Status.ERROR, exit_code=-1, output="",
            )

        exit_code, output = await agent_inst.wait(pid)

        # Did the actor get discarded mid-wait? Row deletion cascades
        # to the run. Match the original semantics: emit
        # `actor_discarded` (with run_id) and return without trying
        # to update a row that's no longer there.
        if not await self._db_call(self._db.actor_exists, actor_name):
            await self.publish_notification(Notification(
                actor=actor_name,
                event="actor_discarded",
                run_id=run_id,
                output=output,
            ))
            return RunResult(
                run_id=run_id, actor=actor_name,
                status=Status.STOPPED, exit_code=exit_code, output=output,
            )

        # Race with `stop_actor`: if the row is already STOPPED, don't
        # overwrite — `stop_actor` writes STOPPED before sending the
        # signal precisely so we can detect it here.
        current = await self._db_call(self._db.latest_run, actor_name)
        if (
            current is not None
            and current.id == run_id
            and current.status == Status.STOPPED
        ):
            final = Status.STOPPED
        else:
            final = Status.DONE if exit_code == 0 else Status.ERROR
            await self._db_call(
                self._db.update_run_status, run_id, final, exit_code,
            )

        # after-run hook fires AFTER the DB has been updated with the
        # final status so a hook that runs `actor show` sees the
        # completed run. Non-zero exit logs to stderr but does NOT
        # fail the run — the agent has already finished.
        after_run = self._hooks().after_run
        if after_run is not None:
            refreshed = await self._db_call(self._db.get_actor, actor_name)
            start = _parse_iso(run_row.started_at)
            end = _parse_iso(_now_iso())
            duration_ms = None
            if start is not None and end is not None:
                duration_ms = max(0, int((end - start).total_seconds() * 1000))
            env = hook_env(
                os.environ,
                actor_name=actor_name,
                actor_dir=Path(actor.dir),
                actor_agent=refreshed.agent.value,
                actor_session_id=refreshed.agent_session,
                actor_run_id=run_id,
                actor_exit_code=exit_code,
                actor_duration_ms=duration_ms,
            )
            try:
                await run_hook(
                    "after-run", after_run, env, Path(actor.dir),
                    runner=self._hook_runner,
                )
            except HookFailedError as e:
                print(f"warning: {e}", file=sys.stderr)

        await self.publish_notification(Notification(
            actor=actor_name,
            event="run_completed",
            run_id=run_id,
            status=final,
            output=output,
        ))

        return RunResult(
            run_id=run_id, actor=actor_name,
            status=final, exit_code=exit_code, output=output,
        )

    async def run_actor(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunResult:
        handle = await self.start_run(name, prompt, config)
        return await self.wait_for_run(handle.run_id)

    async def stop_actor(self, name: str) -> StopResult:
        # Verify the actor exists.
        await self._db_call(self._db.get_actor, name)

        latest = await self._db_call(self._db.latest_run, name)
        if latest is None or latest.status != Status.RUNNING:
            raise NotRunningError(name)

        pid = latest.pid
        alive = pid is not None and self._proc_mgr.is_alive(pid)

        if not alive:
            # Process already dead — stale run, mark as error.
            await self._db_call(
                self._db.update_run_status, latest.id, Status.ERROR, -1,
            )
            return StopResult(name=name, was_alive=False)

        # Order matters: write STOPPED to the DB BEFORE sending the
        # signal. `wait_for_run` is blocked in `agent.wait(pid)`; the
        # moment the signal lands, the agent exits and the wait
        # task wakes up, reads `db.latest_run`, and re-writes the
        # row based on what it sees. If the DB still showed RUNNING
        # at that moment, the wait task's "non-zero -> ERROR"
        # branch would overwrite our pending STOPPED with ERROR(-15).
        # Writing STOPPED first lets the wait race-check observe the
        # terminal state and return early instead.
        assert pid is not None  # alive==True implies pid is not None
        await self._db_call(
            self._db.update_run_status, latest.id, Status.STOPPED, None,
        )
        try:
            actor = await self._db_call(self._db.get_actor, name)
            agent_inst = self._agent(actor.agent)
            await agent_inst.stop(pid)
        except Exception:
            # Revert the optimistic write so callers see actual state.
            await self._db_call(
                self._db.update_run_status, latest.id, Status.RUNNING, None,
            )
            raise

        return StopResult(name=name, was_alive=True)

    async def _force_stop(self, name: str) -> None:
        """Force-stop a running actor (used by discard). Skips the
        SIGTERM dance — kills directly via the process manager."""
        latest = await self._db_call(self._db.latest_run, name)
        if latest is None or latest.status != Status.RUNNING:
            return
        pid = latest.pid
        if pid is not None and self._proc_mgr.is_alive(pid):
            self._proc_mgr.kill(pid)
        await self._db_call(
            self._db.update_run_status, latest.id, Status.STOPPED, None,
        )

    # -- Interactive -------------------------------------------------------

    async def start_interactive_run(
        self, name: str, *, agent: Optional[Agent] = None,
    ) -> InteractiveRunHandle:
        """`agent` lets the watch app's interactive manager hand in
        the per-call agent it already constructed; if omitted we
        derive one through `agent_factory` like every other
        run-lifecycle method."""
        actor = await self._db_call(self._db.get_actor, name)

        status = await self._db_call(
            self._db.resolve_actor_status, name, self._proc_mgr,
        )
        if status == Status.RUNNING:
            raise IsRunningError(name)

        session_id = actor.agent_session
        if session_id is None:
            raise ActorError(
                f"'{name}' has no session yet — "
                f"run it non-interactively first"
            )

        if not binary_exists(actor.agent.binary_name):
            raise AgentNotFoundError(actor.agent.binary_name)

        dir_path = Path(actor.dir)
        if not dir_path.is_dir():
            raise ActorError(
                f"actor directory '{actor.dir}' does not exist — "
                f"use 'actor discard {name}' to clean up"
            )

        # before-run hook mirrors `start_run`. Fires before the Run
        # row is inserted.
        before_run = self._hooks().before_run
        if before_run is not None:
            env = hook_env(
                os.environ,
                actor_name=name,
                actor_dir=dir_path,
                actor_agent=actor.agent.value,
                actor_session_id=actor.agent_session,
            )
            await run_hook(
                "before-run", before_run, env, dir_path,
                runner=self._hook_runner,
            )

        agent_inst = agent if agent is not None else self._agent(actor.agent)
        argv = agent_inst.interactive_argv(session_id, actor.config)

        run = Run(
            id=0,
            actor_name=name,
            prompt=INTERACTIVE_PROMPT,
            status=Status.RUNNING,
            exit_code=None,
            pid=None,
            config=actor.config,
            started_at=_now_iso(),
            finished_at=None,
        )
        run_id = await self._db_call(self._db.insert_run, run)
        await self._db_call(self._db.touch_actor, name)

        return InteractiveRunHandle(
            run_id=run_id,
            session_id=session_id,
            dir=dir_path,
            argv=argv,
        )

    async def update_interactive_run_pid(self, run_id: int, pid: int) -> None:
        await self._db_call(self._db.update_run_pid, run_id, pid)

    async def finalize_interactive_run(
        self,
        run_id: int,
        exit_code: int,
        *,
        force_status: Optional[Status] = None,
    ) -> None:
        """Idempotent: terminal states are not overwritten. Errors
        during finalize log a warning but don't raise — the caller
        runs in a teardown path where we can't recover anyway.

        `force_status` overrides the natural DONE/ERROR derivation:
        the watch manager passes `Status.STOPPED` for app-initiated
        teardown so the row distinguishes `quit watch` from a child
        exiting on its own."""
        run = await self._db_call(self._db.get_run, run_id)
        if run is None:
            return
        if run.status in (Status.DONE, Status.ERROR, Status.STOPPED):
            return

        actor_name = run.actor_name
        try:
            actor = await self._db_call(self._db.get_actor, actor_name)
        except ActorError:
            actor = None

        if force_status is not None:
            final = force_status
            try:
                await self._db_call(
                    self._db.update_run_status, run_id, final, exit_code,
                )
            except ActorError:
                return
        else:
            # Stop race: same logic as `wait_for_run`.
            current = await self._db_call(self._db.latest_run, actor_name)
            if (
                current is not None
                and current.id == run_id
                and current.status == Status.STOPPED
            ):
                final = Status.STOPPED
            else:
                final = Status.DONE if exit_code == 0 else Status.ERROR
                try:
                    await self._db_call(
                        self._db.update_run_status, run_id, final, exit_code,
                    )
                except ActorError:
                    return

        # after-run hook mirrors `wait_for_run`.
        after_run = self._hooks().after_run
        if after_run is not None and actor is not None:
            try:
                refreshed = await self._db_call(self._db.get_actor, actor_name)
            except ActorError:
                refreshed = actor
            start = _parse_iso(run.started_at)
            end = _parse_iso(_now_iso())
            duration_ms = None
            if start is not None and end is not None:
                duration_ms = max(0, int((end - start).total_seconds() * 1000))
            env = hook_env(
                os.environ,
                actor_name=actor_name,
                actor_dir=Path(actor.dir),
                actor_agent=refreshed.agent.value,
                actor_session_id=refreshed.agent_session,
                actor_run_id=run_id,
                actor_exit_code=exit_code,
                actor_duration_ms=duration_ms,
            )
            try:
                await run_hook(
                    "after-run", after_run, env, Path(actor.dir),
                    runner=self._hook_runner,
                )
            except HookFailedError as e:
                print(f"warning: {e}", file=sys.stderr)

        await self.publish_notification(Notification(
            actor=actor_name,
            event="run_completed",
            run_id=run_id,
            status=final,
        ))

    async def interactive_actor(
        self,
        name: str,
        runner: Optional[Callable[[List[str], Path, dict], int]] = None,
    ) -> Tuple[int, str]:
        handle = await self.start_interactive_run(name)

        env = dict(os.environ)
        env["ACTOR_NAME"] = name

        # The runner is a sync, blocking call (it owns the caller's
        # TTY). Hand it off to a worker thread so the asyncio loop
        # stays responsive — though for the CLI path the loop has
        # nothing else to do anyway.
        active_runner = runner or _default_interactive_runner
        try:
            exit_code = await asyncio.to_thread(
                active_runner, handle.argv, handle.dir, env,
            )
        except BaseException:
            try:
                await self._db_call(
                    self._db.update_run_status, handle.run_id, Status.ERROR, -1,
                )
            except ActorError:
                pass
            raise

        await self.finalize_interactive_run(handle.run_id, exit_code)

        # Build the closing message based on whether stop_actor raced
        # us. If the row is STOPPED at this point, finalize honored it;
        # otherwise it's DONE/ERROR per exit_code.
        run = await self._db_call(self._db.get_run, handle.run_id)
        if run is not None and run.status == Status.STOPPED:
            return exit_code, f"Interactive session for '{name}' stopped."
        return exit_code, f"Interactive session for '{name}' ended (exit {exit_code})."

    # -- Discovery ---------------------------------------------------------

    async def get_actor(self, name: str) -> Actor:
        return await self._db_call(self._db.get_actor, name)

    async def actor_exists(self, name: str) -> bool:
        return await self._db_call(self._db.actor_exists, name)

    async def list_actors(self, status_filter: Optional[str] = None) -> List[Actor]:
        actors = await self._db_call(self._db.list_actors)
        if status_filter is None:
            return actors
        # Validate filter early — `Status.from_str` raises on bogus.
        target = Status.from_str(status_filter)
        kept: List[Actor] = []
        for a in actors:
            resolved = await self._db_call(
                self._db.resolve_actor_status, a.name, self._proc_mgr,
            )
            if resolved == target:
                kept.append(a)
        return kept

    async def actor_status(self, name: str) -> Status:
        return await self._db_call(
            self._db.resolve_actor_status, name, self._proc_mgr,
        )

    async def latest_run(self, actor_name: str) -> Optional[Run]:
        return await self._db_call(self._db.latest_run, actor_name)

    async def show_actor(self, name: str, runs_limit: int = 5) -> ActorDetail:
        actor = await self._db_call(self._db.get_actor, name)
        status = await self._db_call(
            self._db.resolve_actor_status, name, self._proc_mgr,
        )
        if runs_limit == 0:
            return ActorDetail(
                actor=actor, status=status, runs=[],
                total_runs=0, runs_limit=0,
            )
        runs, total = await self._db_call(self._db.list_runs, name, runs_limit)
        return ActorDetail(
            actor=actor, status=status, runs=runs,
            total_runs=total, runs_limit=runs_limit,
        )

    async def list_runs(self, actor_name: str, limit: int) -> Tuple[List[Run], int]:
        return await self._db_call(self._db.list_runs, actor_name, limit)

    async def get_run(self, run_id: int) -> Optional[Run]:
        return await self._db_call(self._db.get_run, run_id)

    async def get_logs(self, actor_name: str) -> LogsResult:
        actor = await self._db_call(self._db.get_actor, actor_name)
        session_id = actor.agent_session
        if session_id is None:
            return LogsResult(session_id=None, entries=[])
        agent_inst = self._agent(actor.agent)
        entries = await agent_inst.read_logs(Path(actor.dir), session_id)
        return LogsResult(session_id=session_id, entries=entries)

    async def list_roles(self) -> Dict[str, Role]:
        return self._roles_dict()

    # -- Notifications -----------------------------------------------------

    async def publish_notification(self, n: Notification) -> None:
        # Snapshot under no lock — handler list mutates only via
        # subscribe / cancel, both called from the same loop.
        #
        # Async handlers are awaited inline (sequentially) rather
        # than scheduled as background tasks. The caller depends on
        # all handlers having observed the event by the time
        # `publish_notification` returns — e.g. the MCP server's
        # `_spawn_background_run` cancels its subscription right
        # after `run_actor` returns. Scheduling tasks would race the
        # cancellation and silently drop the channel notification.
        for h in list(self._handlers):
            try:
                if inspect.iscoroutinefunction(h):
                    await h(n)
                else:
                    result = h(n)
                    # Defensive: a sync handler that *returns* a
                    # coroutine (e.g. wraps an async fn) gets
                    # awaited too.
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as e:
                # Never let a misbehaving handler take down the
                # publishing task (especially for run completion
                # tasks in the MCP server).
                print(
                    f"[actor] notification handler raised: {e}",
                    file=sys.stderr,
                )

    async def subscribe_notifications(
        self, handler: NotificationHandler,
    ) -> Cancel:
        self._handlers.append(handler)

        def cancel() -> None:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass

        return cancel


# ---------------------------------------------------------------------------
# RemoteActorService
# ---------------------------------------------------------------------------


class RemoteActorService(ActorService):
    """gRPC client for actord (issue #35, Phase 2.5).

    Wraps the betterproto-generated `ActorServiceStub` and translates
    its protobuf messages back into the same domain types
    `LocalActorService` returns. Per-call short-lived `Channel`s for
    unary methods (matches the Phase 1+2 pattern); `subscribe_notifications`
    keeps a long-lived channel for server-streaming; `interactive_session`
    keeps a long-lived channel for bidi streaming.

    Per-call context (caller cwd, parent actor name) rides on gRPC
    metadata, not on every request body. See `actor.wire` for the
    header keys.
    """

    def __init__(self, transport_uri: str) -> None:
        scheme, target = _parse_transport_uri(transport_uri)
        if scheme != "unix":
            raise NotImplementedError(
                f"transport {scheme!r} not yet supported; see #35 phase 4"
            )
        self._uri = transport_uri
        self._socket_path = target

    def _metadata(self) -> Dict[str, str]:
        from . import wire
        meta: Dict[str, str] = {wire.META_CALLER_CWD: str(Path.cwd())}
        caller = os.environ.get("ACTOR_NAME")
        if caller:
            meta[wire.META_CALLER_ACTOR_NAME] = caller
        return meta

    @asynccontextmanager
    async def _channel(self):
        """Open a unix-socket gRPC channel. The pair of `host="."`,
        `port=None`, `path=...` tells grpclib's `Channel` to use
        AF_UNIX. Closes the channel on exit."""
        from grpclib.client import Channel
        try:
            chan = Channel(path=self._socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonUnreachableError(self._socket_path, e) from e
        try:
            yield chan
        finally:
            chan.close()

    async def _stub_call(self, method_name: str, *args, **kwargs):
        """Run a unary RPC against a fresh channel; translate
        connection errors and `GRPCError`s back to their typed
        ActorError counterparts."""
        from grpclib.exceptions import GRPCError, StreamTerminatedError
        from . import wire
        from ._proto.actor.v1 import ActorServiceStub

        async with self._channel() as chan:
            stub = ActorServiceStub(chan)
            try:
                return await getattr(stub, method_name)(
                    *args, metadata=self._metadata(), **kwargs,
                )
            except GRPCError as exc:
                type_name = None
                if exc.details and isinstance(exc.details, dict):
                    type_name = exc.details.get(wire.META_ERROR_TYPE)
                wire.raise_from_grpc(exc, type_name)
                raise  # unreachable; raise_from_grpc always raises
            except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
                raise DaemonUnreachableError(self._socket_path, e) from e
            except StreamTerminatedError as e:
                raise DaemonUnreachableError(self._socket_path, e) from e

    # -- Lifecycle ---------------------------------------------------------

    async def new_actor(
        self,
        name: str,
        dir: Optional[str],
        no_worktree: bool,
        base: Optional[str],
        agent_name: Optional[str],
        config: ActorConfig,
        role_name: Optional[str] = None,
    ) -> Actor:
        from . import wire
        from ._proto.actor.v1 import NewActorRequest
        resp = await self._stub_call("new_actor", NewActorRequest(
            name=name, dir=dir, no_worktree=no_worktree, base=base,
            agent_name=agent_name,
            config=wire.actor_config_to_pb(config),
            role_name=role_name,
        ))
        return wire.actor_from_pb(resp.actor)

    async def discard_actor(self, name: str, force: bool = False) -> DiscardResult:
        from . import wire
        from ._proto.actor.v1 import DiscardActorRequest
        resp = await self._stub_call("discard_actor", DiscardActorRequest(
            name=name, force=force,
        ))
        return wire.discard_result_from_pb(resp.result)

    async def config_actor(
        self, name: str, pairs: Optional[List[str]] = None,
    ) -> ActorConfig:
        from . import wire
        from ._proto.actor.v1 import ConfigActorRequest
        resp = await self._stub_call("config_actor", ConfigActorRequest(
            name=name, pairs=list(pairs) if pairs else [],
        ))
        return wire.actor_config_from_pb(resp.config)

    # -- Run lifecycle -----------------------------------------------------

    async def start_run(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunStartResult:
        from . import wire
        from ._proto.actor.v1 import StartRunRequest
        resp = await self._stub_call("start_run", StartRunRequest(
            name=name, prompt=prompt,
            config=wire.actor_config_to_pb(config),
        ))
        return wire.run_start_result_from_pb(resp.result)

    async def wait_for_run(self, run_id: int) -> RunResult:
        from . import wire
        from ._proto.actor.v1 import WaitForRunRequest
        resp = await self._stub_call("wait_for_run", WaitForRunRequest(run_id=run_id))
        return wire.run_result_from_pb(resp.result)

    async def run_actor(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunResult:
        from . import wire
        from ._proto.actor.v1 import RunActorRequest
        resp = await self._stub_call("run_actor", RunActorRequest(
            name=name, prompt=prompt,
            config=wire.actor_config_to_pb(config),
        ))
        return wire.run_result_from_pb(resp.result)

    async def stop_actor(self, name: str) -> StopResult:
        from . import wire
        from ._proto.actor.v1 import StopActorRequest
        resp = await self._stub_call("stop_actor", StopActorRequest(name=name))
        return wire.stop_result_from_pb(resp.result)

    # -- Discovery ---------------------------------------------------------

    async def get_actor(self, name: str) -> Actor:
        from . import wire
        from ._proto.actor.v1 import GetActorRequest
        resp = await self._stub_call("get_actor", GetActorRequest(name=name))
        return wire.actor_from_pb(resp.actor)

    async def actor_exists(self, name: str) -> bool:
        from ._proto.actor.v1 import ActorExistsRequest
        resp = await self._stub_call("actor_exists", ActorExistsRequest(name=name))
        return bool(resp.exists)

    async def list_actors(self, status_filter: Optional[str] = None) -> List[Actor]:
        from . import wire
        from ._proto.actor.v1 import ListActorsRequest
        resp = await self._stub_call(
            "list_actors", ListActorsRequest(status_filter=status_filter),
        )
        return [wire.actor_from_pb(a) for a in resp.actors]

    async def actor_status(self, name: str) -> Status:
        from . import wire
        from ._proto.actor.v1 import ActorStatusRequest
        resp = await self._stub_call(
            "actor_status", ActorStatusRequest(name=name),
        )
        return wire.status_from_pb(resp.status)

    async def latest_run(self, actor_name: str) -> Optional[Run]:
        from . import wire
        from ._proto.actor.v1 import LatestRunRequest
        resp = await self._stub_call(
            "latest_run", LatestRunRequest(actor_name=actor_name),
        )
        return wire.run_from_pb(resp.run) if resp.run is not None else None

    async def show_actor(self, name: str, runs_limit: int = 5) -> ActorDetail:
        from . import wire
        from ._proto.actor.v1 import ShowActorRequest
        resp = await self._stub_call(
            "show_actor", ShowActorRequest(name=name, runs_limit=runs_limit),
        )
        return wire.actor_detail_from_pb(resp.detail)

    async def list_runs(self, actor_name: str, limit: int) -> Tuple[List[Run], int]:
        from . import wire
        from ._proto.actor.v1 import ListRunsRequest
        resp = await self._stub_call(
            "list_runs", ListRunsRequest(actor_name=actor_name, limit=limit),
        )
        return [wire.run_from_pb(r) for r in resp.runs], resp.total

    async def get_run(self, run_id: int) -> Optional[Run]:
        from . import wire
        from ._proto.actor.v1 import GetRunRequest
        resp = await self._stub_call("get_run", GetRunRequest(run_id=run_id))
        return wire.run_from_pb(resp.run) if resp.run is not None else None

    async def get_logs(self, actor_name: str) -> LogsResult:
        from . import wire
        from ._proto.actor.v1 import GetLogsRequest
        resp = await self._stub_call(
            "get_logs", GetLogsRequest(actor_name=actor_name),
        )
        return wire.logs_result_from_pb(resp.result)

    async def list_roles(self) -> Dict[str, Role]:
        from . import wire
        from ._proto.actor.v1 import ListRolesRequest
        resp = await self._stub_call("list_roles", ListRolesRequest())
        return {name: wire.role_from_pb(r) for name, r in resp.roles.items()}

    # -- Notifications -----------------------------------------------------

    async def publish_notification(self, n: Notification) -> None:
        from . import wire
        from ._proto.actor.v1 import PublishNotificationRequest
        await self._stub_call(
            "publish_notification",
            PublishNotificationRequest(notification=wire.notification_to_pb(n)),
        )

    async def subscribe_notifications(
        self, handler: NotificationHandler,
    ) -> Cancel:
        """Open a long-lived gRPC server-streaming call and forward
        each `Notification` to `handler` until cancel.

        The returned `Cancel` closes the channel (which the daemon
        notices via the half-closed stream and drops the subscription).
        Cancel is sync to match the existing contract; teardown
        happens on the running loop as a fire-and-forget task."""
        from grpclib.client import Channel
        from grpclib.exceptions import GRPCError, StreamTerminatedError
        from . import wire
        from ._proto.actor.v1 import (
            ActorServiceStub,
            SubscribeNotificationsRequest,
        )

        try:
            chan = Channel(path=self._socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonUnreachableError(self._socket_path, e) from e

        try:
            stub = ActorServiceStub(chan)
            # `subscribe_notifications` is a server-streaming RPC; the
            # generated stub returns an async-iterator. Wrapping the
            # await-iterator dance lets us probe for a connection
            # failure right away.
            stream_cm = stub.subscribe_notifications.open(metadata=self._metadata())
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            chan.close()
            raise DaemonUnreachableError(self._socket_path, e) from e

        async def _reader() -> None:
            try:
                async with stream_cm as stream:
                    await stream.send_message(
                        SubscribeNotificationsRequest(), end=True,
                    )
                    async for pb_n in stream:
                        n = wire.notification_from_pb(pb_n)
                        try:
                            if inspect.iscoroutinefunction(handler):
                                await handler(n)
                            else:
                                result = handler(n)
                                if asyncio.iscoroutine(result):
                                    await result
                        except Exception as e:
                            print(
                                f"[actor] subscriber handler raised: {e}",
                                file=sys.stderr,
                            )
            except (asyncio.CancelledError, GRPCError, StreamTerminatedError):
                pass
            except Exception as e:
                print(
                    f"[actor] subscribe stream error: {e}",
                    file=sys.stderr,
                )
            finally:
                chan.close()

        task = asyncio.create_task(_reader())

        def cancel() -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(_subscribe_teardown(chan, task))

        return cancel

    # -- Interactive (gRPC bidi) -------------------------------------------

    def interactive_session(
        self,
        actor_name: str,
        *,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> "InteractiveSession":
        """Return an `InteractiveSession` handle. Use as an async
        context manager:

            async with svc.interactive_session("alice", cols=80, rows=24) as s:
                # spawn tasks that:
                #   - await s.recv() and write bytes to local stdout/stderr
                #   - read local stdin and call s.send_stdin(data)
                #   - call s.send_resize(...) on SIGWINCH
                #   - call s.send_signal(...) on Ctrl-C
                # then await s.exit_code() to learn the child's status
        """
        return InteractiveSession(
            socket_path=self._socket_path,
            metadata=self._metadata(),
            actor_name=actor_name,
            cols=cols,
            rows=rows,
        )


async def _subscribe_teardown(chan, task) -> None:
    try:
        chan.close()
    except Exception:
        pass
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def _parse_transport_uri(uri: str) -> Tuple[str, str]:
    """Parse `unix:/path/to/sock` or `unix:~/...sock`. Mirrors
    `daemon.parse_transport_uri` so client + daemon agree."""
    if ":" not in uri:
        raise ValueError(f"transport URI missing scheme: {uri!r}")
    scheme, _, target = uri.partition(":")
    if scheme == "unix":
        if not target:
            raise ValueError(f"unix transport URI missing path: {uri!r}")
        return ("unix", os.path.expanduser(target))
    if scheme == "tcp":
        raise NotImplementedError(
            f"tcp transport not yet supported (URI: {uri!r}); "
            f"see issue #35 phase 4"
        )
    raise ValueError(f"unknown transport scheme: {scheme!r}")


# ---------------------------------------------------------------------------
# Interactive session client handle
# ---------------------------------------------------------------------------


class InteractiveSession:
    """Client-side handle for the bidirectional `InteractiveSession`
    RPC. Hides the gRPC stream plumbing behind four send methods +
    one recv iterator, so CLI's `-i` and watch's interactive widget
    can drive it identically.

    Lifecycle:
      __aenter__ → opens the channel + stream, sends OpenSession.
      send_stdin / send_resize / send_signal → push ClientFrames.
      recv() → await next ServerFrame; returns None on stream end.
      exit_code() → resolves to the child's exit code once the
        ExitInfo ServerFrame arrives.
      __aexit__ → cancels outstanding work and closes the channel.
    """

    def __init__(
        self,
        *,
        socket_path: str,
        metadata: Dict[str, str],
        actor_name: str,
        cols: Optional[int],
        rows: Optional[int],
    ) -> None:
        self._socket_path = socket_path
        self._metadata = metadata
        self._actor_name = actor_name
        self._cols = cols
        self._rows = rows
        self._chan = None
        self._stream_cm = None
        self._stream = None
        self._exit_code: Optional[int] = None
        self._final_status: Optional[Status] = None
        self._exit_event = asyncio.Event()

    async def __aenter__(self) -> "InteractiveSession":
        from grpclib.client import Channel
        from ._proto.actor.v1 import (
            ActorServiceStub,
            ClientFrame,
            OpenSession,
        )

        try:
            self._chan = Channel(path=self._socket_path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonUnreachableError(self._socket_path, e) from e

        stub = ActorServiceStub(self._chan)
        self._stream_cm = stub.interactive_session.open(metadata=self._metadata)
        self._stream = await self._stream_cm.__aenter__()
        await self._stream.send_message(ClientFrame(open=OpenSession(
            actor_name=self._actor_name,
            cols=self._cols, rows=self._rows,
        )))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._stream_cm is not None:
                await self._stream_cm.__aexit__(exc_type, exc, tb)
        except Exception:
            pass
        finally:
            if self._chan is not None:
                try:
                    self._chan.close()
                except Exception:
                    pass

    async def send_stdin(self, data: bytes) -> None:
        from ._proto.actor.v1 import ClientFrame
        if not self._stream:
            raise RuntimeError("session not opened")
        await self._stream.send_message(ClientFrame(stdin=data))

    async def send_resize(self, cols: int, rows: int) -> None:
        from ._proto.actor.v1 import ClientFrame, ResizeRequest
        if not self._stream:
            raise RuntimeError("session not opened")
        await self._stream.send_message(ClientFrame(
            resize=ResizeRequest(cols=cols, rows=rows),
        ))

    async def send_signal(self, signal_number: int) -> None:
        from ._proto.actor.v1 import ClientFrame, SignalRequest
        if not self._stream:
            raise RuntimeError("session not opened")
        await self._stream.send_message(ClientFrame(
            signal=SignalRequest(signal_number=signal_number),
        ))

    async def end_input(self) -> None:
        """Half-close the client side. Call once the local stdin
        readers detect EOF (e.g. pipe closure)."""
        if self._stream is not None:
            try:
                await self._stream.end()
            except Exception:
                pass

    async def recv(self):
        """Yield the next ServerFrame, or None when the stream ends.
        Captures ExitInfo internally so callers can rely on
        `exit_code()` after the iterator finishes."""
        from ._proto.actor.v1 import ServerFrame
        if not self._stream:
            return None
        msg = await self._stream.recv_message()
        if msg is None:
            return None
        if msg.exit is not None:
            self._exit_code = msg.exit.exit_code
            from . import wire
            try:
                self._final_status = wire.status_from_pb(msg.exit.final_status)
            except Exception:
                self._final_status = None
            self._exit_event.set()
        return msg

    async def exit_code(self) -> int:
        await self._exit_event.wait()
        assert self._exit_code is not None
        return self._exit_code

    @property
    def final_status(self) -> Optional[Status]:
        return self._final_status
