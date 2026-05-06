"""Domain ↔ protobuf converters + gRPC error mapping (issue #35).

The actord wire is gRPC; `src/actor/_proto/actor/v1/__init__.py` is
the betterproto-generated message + stub layer (regenerate with
`make proto`). This module is the thin adapter between those
generated dataclasses and our internal types/results in
`actor.types`, `actor.interfaces`, `actor.config`, `actor.service`.

Per-call context (caller cwd, parent actor name) rides on gRPC
metadata, not message fields. Header keys are the constants below;
the daemon-side dispatcher reads them, the client-side service sets
them on every outgoing call.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from grpclib.const import Status as GrpcStatus
from grpclib.exceptions import GRPCError

from . import _proto as _  # noqa: F401  ensure package importable
from ._proto.actor import v1 as pb
from .config import Role
from .errors import (
    ActorError,
    AgentNotFoundError,
    AlreadyExistsError,
    ConfigError,
    GitError,
    HookFailedError,
    InvalidNameError,
    IsRunningError,
    NotFoundError,
    NotRunningError,
)
from .interfaces import LogEntry, LogEntryKind
from .service import (
    ActorDetail,
    DiscardResult,
    LogsResult,
    Notification,
    RunResult,
    RunStartResult,
    StopResult,
)
from .types import Actor, ActorConfig, AgentKind, Run, Status


# ---------------------------------------------------------------------------
# Metadata header keys
# ---------------------------------------------------------------------------

# gRPC header names (lowercased; HTTP/2 compresses these well). Kept
# as constants here so daemon + client agree on the spelling and
# typos surface as test failures, not silent drops.
META_CALLER_CWD = "x-actor-caller-cwd"
META_CALLER_ACTOR_NAME = "x-actor-caller-actor-name"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def agent_kind_to_pb(kind: AgentKind) -> pb.AgentKind:
    return {
        AgentKind.CLAUDE: pb.AgentKind.CLAUDE,
        AgentKind.CODEX: pb.AgentKind.CODEX,
    }[kind]


def agent_kind_from_pb(kind: pb.AgentKind) -> AgentKind:
    return {
        pb.AgentKind.CLAUDE: AgentKind.CLAUDE,
        pb.AgentKind.CODEX: AgentKind.CODEX,
    }[kind]


_STATUS_TO_PB = {
    Status.IDLE: pb.Status.IDLE,
    Status.RUNNING: pb.Status.RUNNING,
    Status.DONE: pb.Status.DONE,
    Status.ERROR: pb.Status.ERROR,
    Status.STOPPED: pb.Status.STOPPED,
    Status.INTERACTIVE: pb.Status.INTERACTIVE,
}
_PB_TO_STATUS = {v: k for k, v in _STATUS_TO_PB.items()}


def status_to_pb(s: Status) -> pb.Status:
    return _STATUS_TO_PB[s]


def status_from_pb(s: pb.Status) -> Status:
    if s == pb.Status.UNSPECIFIED:
        raise ValueError("Status.UNSPECIFIED received from wire")
    return _PB_TO_STATUS[s]


_LOG_KIND_TO_PB = {
    LogEntryKind.USER: pb.LogEntryKind.USER,
    LogEntryKind.ASSISTANT: pb.LogEntryKind.ASSISTANT,
    LogEntryKind.THINKING: pb.LogEntryKind.THINKING,
    LogEntryKind.TOOL_USE: pb.LogEntryKind.TOOL_USE,
    LogEntryKind.TOOL_RESULT: pb.LogEntryKind.TOOL_RESULT,
}
_PB_TO_LOG_KIND = {v: k for k, v in _LOG_KIND_TO_PB.items()}


def log_kind_to_pb(k: LogEntryKind) -> pb.LogEntryKind:
    return _LOG_KIND_TO_PB[k]


def log_kind_from_pb(k: pb.LogEntryKind) -> LogEntryKind:
    return _PB_TO_LOG_KIND[k]


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


def actor_config_to_pb(cfg: ActorConfig) -> pb.ActorConfig:
    return pb.ActorConfig(
        actor_keys=dict(cfg.actor_keys),
        agent_args=dict(cfg.agent_args),
    )


def actor_config_from_pb(cfg: Optional[pb.ActorConfig]) -> ActorConfig:
    if cfg is None:
        return ActorConfig()
    return ActorConfig(
        actor_keys=dict(cfg.actor_keys),
        agent_args=dict(cfg.agent_args),
    )


def actor_to_pb(actor: Actor) -> pb.Actor:
    return pb.Actor(
        name=actor.name,
        agent=agent_kind_to_pb(actor.agent),
        agent_session=actor.agent_session,
        dir=actor.dir,
        source_repo=actor.source_repo,
        base_branch=actor.base_branch,
        worktree=actor.worktree,
        parent=actor.parent,
        config=actor_config_to_pb(actor.config),
        created_at=actor.created_at,
        updated_at=actor.updated_at,
    )


def actor_from_pb(actor: pb.Actor) -> Actor:
    return Actor(
        name=actor.name,
        agent=agent_kind_from_pb(actor.agent),
        agent_session=actor.agent_session,
        dir=actor.dir,
        source_repo=actor.source_repo,
        base_branch=actor.base_branch,
        worktree=actor.worktree,
        parent=actor.parent,
        config=actor_config_from_pb(actor.config),
        created_at=actor.created_at,
        updated_at=actor.updated_at,
    )


def run_to_pb(run: Run) -> pb.Run:
    return pb.Run(
        id=run.id,
        actor_name=run.actor_name,
        prompt=run.prompt,
        status=status_to_pb(run.status),
        exit_code=run.exit_code,
        pid=run.pid,
        config=actor_config_to_pb(run.config),
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def run_from_pb(run: pb.Run) -> Run:
    return Run(
        id=run.id,
        actor_name=run.actor_name,
        prompt=run.prompt,
        status=status_from_pb(run.status),
        exit_code=run.exit_code,
        pid=run.pid,
        config=actor_config_from_pb(run.config),
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def role_to_pb(role: Role) -> pb.Role:
    return pb.Role(
        name=role.name,
        agent=role.agent,
        prompt=role.prompt,
        description=role.description,
        config=dict(role.config),
    )


def role_from_pb(role: pb.Role) -> Role:
    return Role(
        name=role.name,
        agent=role.agent,
        prompt=role.prompt,
        description=role.description,
        config=dict(role.config),
    )


def log_entry_to_pb(e: LogEntry) -> pb.LogEntry:
    has_usage = e.usage is not None
    return pb.LogEntry(
        kind=log_kind_to_pb(e.kind),
        timestamp=e.timestamp,
        text=e.text,
        name=e.name,
        input=e.input,
        content=e.content,
        usage=dict(e.usage) if has_usage else {},
        has_usage=has_usage,
    )


def log_entry_from_pb(e: pb.LogEntry) -> LogEntry:
    return LogEntry(
        kind=log_kind_from_pb(e.kind),
        timestamp=e.timestamp,
        text=e.text,
        name=e.name,
        input=e.input,
        content=e.content,
        usage=dict(e.usage) if e.has_usage else None,
    )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


def discard_result_to_pb(r: DiscardResult) -> pb.DiscardResult:
    return pb.DiscardResult(names=list(r.names))


def discard_result_from_pb(r: pb.DiscardResult) -> DiscardResult:
    return DiscardResult(names=list(r.names))


def run_start_result_to_pb(r: RunStartResult) -> pb.RunStartResult:
    return pb.RunStartResult(
        run_id=r.run_id,
        pid=r.pid,
        status=status_to_pb(r.status),
    )


def run_start_result_from_pb(r: pb.RunStartResult) -> RunStartResult:
    return RunStartResult(
        run_id=r.run_id,
        pid=r.pid,
        status=status_from_pb(r.status),
    )


def run_result_to_pb(r: RunResult) -> pb.RunResult:
    return pb.RunResult(
        run_id=r.run_id,
        actor=r.actor,
        status=status_to_pb(r.status),
        exit_code=r.exit_code,
        output=r.output,
    )


def run_result_from_pb(r: pb.RunResult) -> RunResult:
    return RunResult(
        run_id=r.run_id,
        actor=r.actor,
        status=status_from_pb(r.status),
        exit_code=r.exit_code,
        output=r.output,
    )


def stop_result_to_pb(r: StopResult) -> pb.StopResult:
    return pb.StopResult(name=r.name, was_alive=r.was_alive)


def stop_result_from_pb(r: pb.StopResult) -> StopResult:
    return StopResult(name=r.name, was_alive=r.was_alive)


def actor_detail_to_pb(d: ActorDetail) -> pb.ActorDetail:
    return pb.ActorDetail(
        actor=actor_to_pb(d.actor),
        status=status_to_pb(d.status),
        runs=[run_to_pb(r) for r in d.runs],
        total_runs=d.total_runs,
        runs_limit=d.runs_limit,
    )


def actor_detail_from_pb(d: pb.ActorDetail) -> ActorDetail:
    return ActorDetail(
        actor=actor_from_pb(d.actor),
        status=status_from_pb(d.status),
        runs=[run_from_pb(r) for r in d.runs],
        total_runs=d.total_runs,
        runs_limit=d.runs_limit,
    )


def logs_result_to_pb(r: LogsResult) -> pb.LogsResult:
    return pb.LogsResult(
        session_id=r.session_id,
        entries=[log_entry_to_pb(e) for e in r.entries],
    )


def logs_result_from_pb(r: pb.LogsResult) -> LogsResult:
    return LogsResult(
        session_id=r.session_id,
        entries=[log_entry_from_pb(e) for e in r.entries],
    )


def notification_to_pb(n: Notification) -> pb.Notification:
    return pb.Notification(
        actor=n.actor,
        event=n.event,
        run_id=n.run_id,
        status=status_to_pb(n.status) if n.status is not None else None,
        output=n.output,
        timestamp=float(n.timestamp),
    )


def notification_from_pb(n: pb.Notification) -> Notification:
    return Notification(
        actor=n.actor,
        event=n.event,  # type: ignore[arg-type]
        run_id=n.run_id,
        status=status_from_pb(n.status) if n.status is not None else None,
        output=n.output,
        timestamp=n.timestamp,
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

# `data.type = ClassName` rides in trailing metadata so the client can
# re-raise the original ActorError subclass (matches the JSON-RPC
# typed-error UX from Phase 2). Everything still maps to a sane gRPC
# status code on top of that.
META_ERROR_TYPE = "x-actor-error-type"


_STATUS_BY_TYPE: Dict[type, GrpcStatus] = {
    NotFoundError: GrpcStatus.NOT_FOUND,
    AlreadyExistsError: GrpcStatus.ALREADY_EXISTS,
    IsRunningError: GrpcStatus.FAILED_PRECONDITION,
    NotRunningError: GrpcStatus.FAILED_PRECONDITION,
    InvalidNameError: GrpcStatus.INVALID_ARGUMENT,
    AgentNotFoundError: GrpcStatus.FAILED_PRECONDITION,
    GitError: GrpcStatus.INTERNAL,
    ConfigError: GrpcStatus.INVALID_ARGUMENT,
    HookFailedError: GrpcStatus.ABORTED,
}


_REGISTRY: Dict[str, type] = {
    cls.__name__: cls for cls in [
        ActorError,
        AlreadyExistsError,
        NotFoundError,
        IsRunningError,
        NotRunningError,
        InvalidNameError,
        AgentNotFoundError,
        GitError,
        ConfigError,
        HookFailedError,
    ]
}


def actor_error_to_grpc(exc: BaseException) -> GRPCError:
    """Translate an `ActorError` (or anything else) into a `GRPCError`
    the daemon can raise from a handler. The original class name
    rides as a trailing metadata header so the client can rebuild a
    typed exception."""
    if isinstance(exc, ActorError):
        status = _STATUS_BY_TYPE.get(type(exc), GrpcStatus.INTERNAL)
        message = str(exc)
        type_name = type(exc).__name__
    else:
        status = GrpcStatus.INTERNAL
        message = repr(exc)
        type_name = type(exc).__name__
    err = GRPCError(status, message)
    # Stash the class name so the client can re-raise the same type.
    # `GRPCError` doesn't model trailing metadata directly; clients
    # read it from the response trailers via grpclib's call object.
    err.actor_error_type = type_name  # type: ignore[attr-defined]
    return err


def raise_from_grpc(err: GRPCError, type_name: Optional[str] = None) -> None:
    """Inverse of `actor_error_to_grpc`. The client passes the type
    name it recovered from trailing metadata (or None if absent).
    Falls back to plain `ActorError` when the type isn't known."""
    cls = _REGISTRY.get(type_name) if type_name else None
    if cls is None:
        # No type info → use the exception class implied by the gRPC
        # status, so callers can `except NotFoundError` even without
        # the trailing metadata header (e.g. an older daemon).
        cls = _STATUS_TO_FALLBACK.get(err.status, ActorError)
    inst = cls.__new__(cls)
    Exception.__init__(inst, err.message or "")
    raise inst


_STATUS_TO_FALLBACK: Dict[GrpcStatus, type] = {
    GrpcStatus.NOT_FOUND: NotFoundError,
    GrpcStatus.ALREADY_EXISTS: AlreadyExistsError,
    GrpcStatus.FAILED_PRECONDITION: ActorError,
    GrpcStatus.INVALID_ARGUMENT: ActorError,
    GrpcStatus.ABORTED: ActorError,
    GrpcStatus.INTERNAL: ActorError,
}


__all__ = [
    "META_CALLER_CWD",
    "META_CALLER_ACTOR_NAME",
    "META_ERROR_TYPE",
    "agent_kind_to_pb",
    "agent_kind_from_pb",
    "status_to_pb",
    "status_from_pb",
    "actor_config_to_pb",
    "actor_config_from_pb",
    "actor_to_pb",
    "actor_from_pb",
    "run_to_pb",
    "run_from_pb",
    "role_to_pb",
    "role_from_pb",
    "log_entry_to_pb",
    "log_entry_from_pb",
    "discard_result_to_pb",
    "discard_result_from_pb",
    "run_start_result_to_pb",
    "run_start_result_from_pb",
    "run_result_to_pb",
    "run_result_from_pb",
    "stop_result_to_pb",
    "stop_result_from_pb",
    "actor_detail_to_pb",
    "actor_detail_from_pb",
    "logs_result_to_pb",
    "logs_result_from_pb",
    "notification_to_pb",
    "notification_from_pb",
    "actor_error_to_grpc",
    "raise_from_grpc",
]
