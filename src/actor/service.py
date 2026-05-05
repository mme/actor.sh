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

Notifications are an in-process pub/sub. Handlers run in whichever
thread published the event; transports that need an asyncio loop (the
MCP server's stdio session) own the bridge themselves.
"""
from __future__ import annotations

import abc
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Tuple

from .agents.claude import ClaudeAgent
from .agents.codex import CodexAgent
from .config import AppConfig, Hooks, Role
from .db import Database
from .errors import (
    ActorError,
    AgentNotFoundError,
    ConfigError,
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
NotificationHandler = Callable[[Notification], None]


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class ActorService(abc.ABC):
    """Single boundary between commands and storage."""

    # -- Lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    def new_actor(
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
    def discard_actor(self, name: str, force: bool = False) -> DiscardResult: ...

    @abc.abstractmethod
    def config_actor(
        self, name: str, pairs: Optional[List[str]] = None,
    ) -> ActorConfig: ...

    # -- Runs --------------------------------------------------------------

    @abc.abstractmethod
    def start_run(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunStartResult: ...

    @abc.abstractmethod
    def wait_for_run(self, run_id: int) -> RunResult: ...

    @abc.abstractmethod
    def run_actor(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunResult: ...

    @abc.abstractmethod
    def stop_actor(self, name: str) -> StopResult: ...

    # -- Interactive -------------------------------------------------------

    @abc.abstractmethod
    def start_interactive_run(
        self, name: str, *, agent: Optional[Agent] = None,
    ) -> InteractiveRunHandle: ...

    @abc.abstractmethod
    def update_interactive_run_pid(self, run_id: int, pid: int) -> None: ...

    @abc.abstractmethod
    def finalize_interactive_run(
        self,
        run_id: int,
        exit_code: int,
        *,
        force_status: Optional[Status] = None,
    ) -> None: ...

    @abc.abstractmethod
    def interactive_actor(
        self,
        name: str,
        runner: Optional[Callable[[List[str], Path, dict], int]] = None,
    ) -> Tuple[int, str]: ...

    # -- Discovery ---------------------------------------------------------

    @abc.abstractmethod
    def get_actor(self, name: str) -> Actor: ...

    @abc.abstractmethod
    def actor_exists(self, name: str) -> bool: ...

    @abc.abstractmethod
    def list_actors(self, status_filter: Optional[str] = None) -> List[Actor]: ...

    @abc.abstractmethod
    def actor_status(self, name: str) -> Status: ...

    @abc.abstractmethod
    def latest_run(self, actor_name: str) -> Optional[Run]: ...

    @abc.abstractmethod
    def show_actor(self, name: str, runs_limit: int = 5) -> ActorDetail: ...

    @abc.abstractmethod
    def list_runs(self, actor_name: str, limit: int) -> Tuple[List[Run], int]: ...

    @abc.abstractmethod
    def get_run(self, run_id: int) -> Optional[Run]: ...

    @abc.abstractmethod
    def get_logs(self, actor_name: str) -> LogsResult: ...

    @abc.abstractmethod
    def list_roles(self) -> Dict[str, Role]: ...

    # -- Notifications -----------------------------------------------------

    @abc.abstractmethod
    def publish_notification(self, n: Notification) -> None: ...

    @abc.abstractmethod
    def subscribe_notifications(
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
        self._handler_lock = threading.Lock()
        # Agents carry per-instance state — `ClaudeAgent._children`
        # tracks subprocess.Popen handles between `start()` and
        # `wait()`. Splitting the run lifecycle into `start_run` /
        # `wait_for_run` means both must see the same agent instance,
        # so we cache one per kind for the lifetime of the service.
        self._agent_cache: Dict[AgentKind, Agent] = {}
        self._agent_cache_lock = threading.Lock()

    # -- Internal helpers --------------------------------------------------

    def _hooks(self) -> Hooks:
        return self._app_config.hooks if self._app_config is not None else Hooks()

    def _roles(self) -> Dict[str, Role]:
        return dict(self._app_config.roles) if self._app_config is not None else {}

    def _agent(self, kind: AgentKind) -> Agent:
        with self._agent_cache_lock:
            inst = self._agent_cache.get(kind)
            if inst is None:
                inst = self._agent_factory(kind)
                self._agent_cache[kind] = inst
            return inst

    # -- Lifecycle ---------------------------------------------------------

    def new_actor(
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
            roles = self._roles()
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

        if self._app_config is not None:
            kdl_defaults = self._app_config.agent_defaults.get(agent_kind.value)
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

        use_worktree = (not no_worktree) and self._git.is_repo(base_dir)

        if not use_worktree:
            actor_dir = str(base_dir)
            source_repo: Optional[str] = None
            base_branch: Optional[str] = None
            worktree = False
        else:
            branch_base = base if base is not None else self._git.current_branch(base_dir)
            wt_path = _worktree_path(name)
            self._git.create_worktree(base_dir, wt_path, name, branch_base)
            actor_dir = str(wt_path)
            source_repo = str(base_dir)
            base_branch = branch_base
            worktree = True

        parent = os.environ.get("ACTOR_NAME")

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
            self._db.insert_actor(actor)
        except ActorError:
            if worktree:
                wt_path = _worktree_path(name)
                try:
                    self._git.remove_worktree(Path(source_repo), wt_path)  # type: ignore[arg-type]
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
                run_hook(
                    "on-start", on_start, env, Path(actor_dir),
                    runner=self._hook_runner,
                )
            except Exception:
                try:
                    self._db.delete_actor(name)
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
                        self._git.remove_worktree(Path(source_repo), wt_path)
                    except Exception as cleanup_err:
                        print(
                            f"warning: failed to clean up worktree at {wt_path}: {cleanup_err}",
                            file=sys.stderr,
                        )
                raise

        return actor

    def discard_actor(self, name: str, force: bool = False) -> DiscardResult:
        """Discard an actor — stops it if running, runs the on-discard
        hook, removes the worktree, deletes the DB row.

        Children (recursively discovered via the `parent` column) are
        processed depth-first — leaves first — so a parent is only
        deleted once all its descendants are gone. If any discard in
        the chain raises, the chain stops and the surfaced exception
        names the actor that broke the chain.
        """
        result = DiscardResult(names=[])
        self._discard_recursive(name, force=force, visited=set(), result=result)
        return result

    def _discard_recursive(
        self,
        name: str,
        *,
        force: bool,
        visited: set,
        result: DiscardResult,
    ) -> None:
        actor = self._db.get_actor(name)
        visited.add(name)

        children = self._db.list_children(name)
        for child in children:
            if child.name not in visited:
                self._discard_recursive(
                    child.name, force=force, visited=visited, result=result,
                )

        # Stop if running. Has to happen BEFORE the hook so the hook
        # runs against settled working-tree state.
        status = self._db.resolve_actor_status(name, self._proc_mgr)
        if status == Status.RUNNING:
            self._force_stop(name)

        # Default `git diff --quiet` ONLY when an `app_config` was
        # wired in — service constructed without a config (some tests)
        # gets no default hook so a stray `git diff` doesn't bite.
        if self._app_config is not None:
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
                run_hook(
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
                    self._git.remove_worktree(Path(actor.source_repo), wt_path)
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

        self._db.delete_actor(name)
        result.names.append(name)
        self.publish_notification(Notification(
            actor=name, event="actor_discarded",
        ))

    def config_actor(
        self, name: str, pairs: Optional[List[str]] = None,
    ) -> ActorConfig:
        actor = self._db.get_actor(name)

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
        self._db.update_actor_config(name, new_config)
        return new_config

    # -- Run lifecycle -----------------------------------------------------

    def start_run(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunStartResult:
        actor = self._db.get_actor(name)

        status = self._db.resolve_actor_status(name, self._proc_mgr)
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
            run_hook(
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
        run_id = self._db.insert_run(run)
        self._db.touch_actor(name)

        # Expose actor name to the agent process (set for child,
        # restored after).
        prev_actor_name = os.environ.get("ACTOR_NAME")
        os.environ["ACTOR_NAME"] = name

        agent_inst = self._agent(actor.agent)
        try:
            if actor.agent_session is not None:
                pid = agent_inst.resume(
                    dir_path, actor.agent_session, prompt, effective_config,
                )
                new_session: Optional[str] = None
            else:
                pid, new_session = agent_inst.start(
                    dir_path, prompt, effective_config,
                )
        except Exception:
            self._db.update_run_status(run_id, Status.ERROR, -1)
            raise
        finally:
            if prev_actor_name is None:
                os.environ.pop("ACTOR_NAME", None)
            else:
                os.environ["ACTOR_NAME"] = prev_actor_name

        self._db.update_run_pid(run_id, pid)
        if new_session is not None:
            self._db.update_actor_session(name, new_session)

        return RunStartResult(run_id=run_id, pid=pid, status=Status.RUNNING)

    def wait_for_run(self, run_id: int) -> RunResult:
        run_row = self._db.get_run(run_id)
        if run_row is None:
            raise ActorError(f"run {run_id} not found")
        actor_name = run_row.actor_name
        actor = self._db.get_actor(actor_name)
        agent_inst = self._agent(actor.agent)

        pid = run_row.pid
        if pid is None:
            # `start_run` writes the pid before returning — landing
            # here means start_run failed in a way that didn't raise.
            try:
                self._db.update_run_status(run_id, Status.ERROR, -1)
            except ActorError:
                pass
            return RunResult(
                run_id=run_id, actor=actor_name,
                status=Status.ERROR, exit_code=-1, output="",
            )

        exit_code, output = agent_inst.wait(pid)

        # Did the actor get discarded mid-wait? Row deletion cascades
        # to the run. Match the original semantics: emit
        # `actor_discarded` (with run_id) and return without trying
        # to update a row that's no longer there.
        if not self._db.actor_exists(actor_name):
            self.publish_notification(Notification(
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
        current = self._db.latest_run(actor_name)
        if (
            current is not None
            and current.id == run_id
            and current.status == Status.STOPPED
        ):
            final = Status.STOPPED
        else:
            final = Status.DONE if exit_code == 0 else Status.ERROR
            self._db.update_run_status(run_id, final, exit_code)

        # after-run hook fires AFTER the DB has been updated with the
        # final status so a hook that runs `actor show` sees the
        # completed run. Non-zero exit logs to stderr but does NOT
        # fail the run — the agent has already finished.
        after_run = self._hooks().after_run
        if after_run is not None:
            refreshed = self._db.get_actor(actor_name)
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
                run_hook(
                    "after-run", after_run, env, Path(actor.dir),
                    runner=self._hook_runner,
                )
            except HookFailedError as e:
                print(f"warning: {e}", file=sys.stderr)

        self.publish_notification(Notification(
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

    def run_actor(
        self, name: str, prompt: str, config: ActorConfig,
    ) -> RunResult:
        handle = self.start_run(name, prompt, config)
        return self.wait_for_run(handle.run_id)

    def stop_actor(self, name: str) -> StopResult:
        # Verify the actor exists.
        self._db.get_actor(name)

        latest = self._db.latest_run(name)
        if latest is None or latest.status != Status.RUNNING:
            raise NotRunningError(name)

        pid = latest.pid
        alive = pid is not None and self._proc_mgr.is_alive(pid)

        if not alive:
            # Process already dead — stale run, mark as error.
            self._db.update_run_status(latest.id, Status.ERROR, -1)
            return StopResult(name=name, was_alive=False)

        # Order matters: write STOPPED to the DB BEFORE sending the
        # signal. `wait_for_run` is blocked in `agent.wait(pid)`; the
        # moment the signal lands, the agent exits and the wait
        # thread wakes up, reads `db.latest_run`, and re-writes the
        # row based on what it sees. If the DB still showed RUNNING
        # at that moment, the wait thread's "non-zero -> ERROR"
        # branch would overwrite our pending STOPPED with ERROR(-15).
        # Writing STOPPED first lets the wait race-check observe the
        # terminal state and return early instead.
        assert pid is not None  # alive==True implies pid is not None
        self._db.update_run_status(latest.id, Status.STOPPED, None)
        try:
            agent_inst = self._agent(self._db.get_actor(name).agent)
            agent_inst.stop(pid)
        except Exception:
            # Revert the optimistic write so callers see actual state.
            self._db.update_run_status(latest.id, Status.RUNNING, None)
            raise

        return StopResult(name=name, was_alive=True)

    def _force_stop(self, name: str) -> None:
        """Force-stop a running actor (used by discard). Skips the
        SIGTERM dance — kills directly via the process manager."""
        latest = self._db.latest_run(name)
        if latest is None or latest.status != Status.RUNNING:
            return
        pid = latest.pid
        if pid is not None and self._proc_mgr.is_alive(pid):
            self._proc_mgr.kill(pid)
        self._db.update_run_status(latest.id, Status.STOPPED, None)

    # -- Interactive -------------------------------------------------------

    def start_interactive_run(
        self, name: str, *, agent: Optional[Agent] = None,
    ) -> InteractiveRunHandle:
        """`agent` lets the watch app's interactive manager hand in
        the per-call agent it already constructed; if omitted we
        derive one through `agent_factory` like every other
        run-lifecycle method."""
        actor = self._db.get_actor(name)

        status = self._db.resolve_actor_status(name, self._proc_mgr)
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
            run_hook(
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
        run_id = self._db.insert_run(run)
        self._db.touch_actor(name)

        return InteractiveRunHandle(
            run_id=run_id,
            session_id=session_id,
            dir=dir_path,
            argv=argv,
        )

    def update_interactive_run_pid(self, run_id: int, pid: int) -> None:
        self._db.update_run_pid(run_id, pid)

    def finalize_interactive_run(
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
        run = self._db.get_run(run_id)
        if run is None:
            return
        if run.status in (Status.DONE, Status.ERROR, Status.STOPPED):
            return

        actor_name = run.actor_name
        try:
            actor = self._db.get_actor(actor_name)
        except ActorError:
            actor = None

        if force_status is not None:
            final = force_status
            try:
                self._db.update_run_status(run_id, final, exit_code)
            except ActorError:
                return
        else:
            # Stop race: same logic as `wait_for_run`.
            current = self._db.latest_run(actor_name)
            if (
                current is not None
                and current.id == run_id
                and current.status == Status.STOPPED
            ):
                final = Status.STOPPED
            else:
                final = Status.DONE if exit_code == 0 else Status.ERROR
                try:
                    self._db.update_run_status(run_id, final, exit_code)
                except ActorError:
                    return

        # after-run hook mirrors `wait_for_run`.
        after_run = self._hooks().after_run
        if after_run is not None and actor is not None:
            try:
                refreshed = self._db.get_actor(actor_name)
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
                run_hook(
                    "after-run", after_run, env, Path(actor.dir),
                    runner=self._hook_runner,
                )
            except HookFailedError as e:
                print(f"warning: {e}", file=sys.stderr)

        self.publish_notification(Notification(
            actor=actor_name,
            event="run_completed",
            run_id=run_id,
            status=final,
        ))

    def interactive_actor(
        self,
        name: str,
        runner: Optional[Callable[[List[str], Path, dict], int]] = None,
    ) -> Tuple[int, str]:
        handle = self.start_interactive_run(name)

        env = dict(os.environ)
        env["ACTOR_NAME"] = name

        try:
            exit_code = (runner or _default_interactive_runner)(
                handle.argv, handle.dir, env,
            )
        except BaseException:
            try:
                self._db.update_run_status(handle.run_id, Status.ERROR, -1)
            except ActorError:
                pass
            raise

        self.finalize_interactive_run(handle.run_id, exit_code)

        # Build the closing message based on whether stop_actor raced
        # us. If the row is STOPPED at this point, finalize honored it;
        # otherwise it's DONE/ERROR per exit_code.
        run = self._db.get_run(handle.run_id)
        if run is not None and run.status == Status.STOPPED:
            return exit_code, f"Interactive session for '{name}' stopped."
        return exit_code, f"Interactive session for '{name}' ended (exit {exit_code})."

    # -- Discovery ---------------------------------------------------------

    def get_actor(self, name: str) -> Actor:
        return self._db.get_actor(name)

    def actor_exists(self, name: str) -> bool:
        return self._db.actor_exists(name)

    def list_actors(self, status_filter: Optional[str] = None) -> List[Actor]:
        actors = self._db.list_actors()
        if status_filter is None:
            return actors
        # Validate filter early — `Status.from_str` raises on bogus.
        target = Status.from_str(status_filter)
        kept: List[Actor] = []
        for a in actors:
            if self._db.resolve_actor_status(a.name, self._proc_mgr) == target:
                kept.append(a)
        return kept

    def actor_status(self, name: str) -> Status:
        return self._db.resolve_actor_status(name, self._proc_mgr)

    def latest_run(self, actor_name: str) -> Optional[Run]:
        return self._db.latest_run(actor_name)

    def show_actor(self, name: str, runs_limit: int = 5) -> ActorDetail:
        actor = self._db.get_actor(name)
        status = self._db.resolve_actor_status(name, self._proc_mgr)
        if runs_limit == 0:
            return ActorDetail(
                actor=actor, status=status, runs=[],
                total_runs=0, runs_limit=0,
            )
        runs, total = self._db.list_runs(name, runs_limit)
        return ActorDetail(
            actor=actor, status=status, runs=runs,
            total_runs=total, runs_limit=runs_limit,
        )

    def list_runs(self, actor_name: str, limit: int) -> Tuple[List[Run], int]:
        return self._db.list_runs(actor_name, limit)

    def get_run(self, run_id: int) -> Optional[Run]:
        return self._db.get_run(run_id)

    def get_logs(self, actor_name: str) -> LogsResult:
        actor = self._db.get_actor(actor_name)
        session_id = actor.agent_session
        if session_id is None:
            return LogsResult(session_id=None, entries=[])
        agent_inst = self._agent(actor.agent)
        entries = agent_inst.read_logs(Path(actor.dir), session_id)
        return LogsResult(session_id=session_id, entries=entries)

    def list_roles(self) -> Dict[str, Role]:
        return self._roles()

    # -- Notifications -----------------------------------------------------

    def publish_notification(self, n: Notification) -> None:
        with self._handler_lock:
            handlers = list(self._handlers)
        for h in handlers:
            try:
                h(n)
            except Exception as e:
                # Never let a misbehaving handler take down the
                # publishing thread (especially for run completion
                # threads in the MCP server).
                print(
                    f"[actor] notification handler raised: {e}",
                    file=sys.stderr,
                )

    def subscribe_notifications(
        self, handler: NotificationHandler,
    ) -> Cancel:
        with self._handler_lock:
            self._handlers.append(handler)

        def cancel() -> None:
            with self._handler_lock:
                try:
                    self._handlers.remove(handler)
                except ValueError:
                    pass

        return cancel
