"""JSON-RPC 2.0 framing + wire (de)serializers for the actord wire (issue #35).

This is wire 2 — clients (CLI, MCP bridge, watch) ↔ `actord`. Pure
stdlib JSON; no MCP SDK on this wire. Wire 1 (Claude Code ↔ `actor
mcp`, stdio MCP) lives in `server.py` and is untouched.

Phase 2 carries the full ActorService surface. The serializers below
mirror service.py's result types 1:1; new fields go through here so
both ends agree.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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
from .types import Actor, ActorConfig, AgentKind, Run, Status


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 standard error codes
# ---------------------------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Server-defined application range (-32000 to -32099).
APPLICATION_ERROR = -32000


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


@dataclass
class JSONRPCRequest:
    id: Union[int, str]
    method: str
    params: Optional[Dict[str, Any]]


@dataclass
class JSONRPCResponse:
    id: Union[int, str]
    result: Any


@dataclass
class JSONRPCNotification:
    """Server → client push (no `id`)."""
    method: str
    params: Optional[Dict[str, Any]]


@dataclass
class JSONRPCError:
    id: Optional[Union[int, str]]
    code: int
    message: str
    data: Any = None


Message = Union[JSONRPCRequest, JSONRPCResponse, JSONRPCNotification, JSONRPCError]


class ProtocolError(Exception):
    """Raised when an inbound frame can't be parsed as JSON-RPC 2.0."""


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def encode_request(req: JSONRPCRequest) -> str:
    obj: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": req.id,
        "method": req.method,
    }
    if req.params is not None:
        obj["params"] = req.params
    return json.dumps(obj)


def encode_response(resp: JSONRPCResponse) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": resp.id,
        "result": resp.result,
    })


def encode_notification(note: JSONRPCNotification) -> str:
    obj: Dict[str, Any] = {"jsonrpc": "2.0", "method": note.method}
    if note.params is not None:
        obj["params"] = note.params
    return json.dumps(obj)


def encode_error(err: JSONRPCError) -> str:
    error_obj: Dict[str, Any] = {"code": err.code, "message": err.message}
    if err.data is not None:
        error_obj["data"] = err.data
    return json.dumps({
        "jsonrpc": "2.0",
        "id": err.id,
        "error": error_obj,
    })


def decode_message(raw: str) -> Message:
    """Parse a single JSON-RPC 2.0 frame.

    Discriminates request / response / notification / error by which
    top-level keys are present. Raises `ProtocolError` for malformed
    input."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON: {e}") from e

    if not isinstance(obj, dict):
        raise ProtocolError("frame must be a JSON object")
    if obj.get("jsonrpc") != "2.0":
        raise ProtocolError("missing or wrong 'jsonrpc' version (expected '2.0')")

    if "method" in obj:
        method = obj["method"]
        if not isinstance(method, str):
            raise ProtocolError("'method' must be a string")
        params = obj.get("params")
        if params is not None and not isinstance(params, dict):
            raise ProtocolError(
                "'params' must be an object (positional params not supported)"
            )
        if "id" in obj:
            return JSONRPCRequest(id=obj["id"], method=method, params=params)
        return JSONRPCNotification(method=method, params=params)

    if "error" in obj:
        err = obj["error"]
        if not isinstance(err, dict):
            raise ProtocolError("'error' must be an object")
        code = err.get("code")
        message = err.get("message")
        if not isinstance(code, int) or not isinstance(message, str):
            raise ProtocolError(
                "'error' must have integer 'code' and string 'message'"
            )
        return JSONRPCError(
            id=obj.get("id"),
            code=code,
            message=message,
            data=err.get("data"),
        )

    if "result" in obj:
        return JSONRPCResponse(id=obj.get("id"), result=obj["result"])

    raise ProtocolError("frame is neither request nor response nor error")


# ---------------------------------------------------------------------------
# Transport URI
# ---------------------------------------------------------------------------


def parse_transport_uri(uri: str) -> Tuple[str, str]:
    """Parse a transport URI into (scheme, target).

    Supported:
        unix:/path/to/sock      → ("unix", "/path/to/sock")
        unix:~/.actor/sock      → ("unix", "/home/.../.actor/sock")
        tcp:host:port           → raises NotImplementedError (Phase 4)

    The unix path is `~`-expanded so callers can pass user-typed URIs
    verbatim. Other schemes raise `ValueError`.
    """
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
# Domain → wire serializers
# ---------------------------------------------------------------------------


def actor_config_to_dict(cfg: ActorConfig) -> Dict[str, Any]:
    return {
        "actor_keys": dict(cfg.actor_keys),
        "agent_args": dict(cfg.agent_args),
    }


def actor_config_from_dict(d: Optional[Dict[str, Any]]) -> ActorConfig:
    d = d or {}
    return ActorConfig(
        actor_keys=dict(d.get("actor_keys") or {}),
        agent_args=dict(d.get("agent_args") or {}),
    )


def actor_to_dict(actor: Actor) -> Dict[str, Any]:
    return {
        "name": actor.name,
        "agent": actor.agent.value,
        "agent_session": actor.agent_session,
        "dir": actor.dir,
        "source_repo": actor.source_repo,
        "base_branch": actor.base_branch,
        "worktree": actor.worktree,
        "parent": actor.parent,
        "config": actor_config_to_dict(actor.config),
        "created_at": actor.created_at,
        "updated_at": actor.updated_at,
    }


def actor_from_dict(d: Dict[str, Any]) -> Actor:
    return Actor(
        name=d["name"],
        agent=AgentKind.from_str(d["agent"]),
        agent_session=d.get("agent_session"),
        dir=d["dir"],
        source_repo=d.get("source_repo"),
        base_branch=d.get("base_branch"),
        worktree=bool(d["worktree"]),
        parent=d.get("parent"),
        config=actor_config_from_dict(d.get("config")),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )


def run_to_dict(run: Run) -> Dict[str, Any]:
    return {
        "id": run.id,
        "actor_name": run.actor_name,
        "prompt": run.prompt,
        "status": run.status.value,
        "exit_code": run.exit_code,
        "pid": run.pid,
        "config": actor_config_to_dict(run.config),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def run_from_dict(d: Dict[str, Any]) -> Run:
    return Run(
        id=d["id"],
        actor_name=d["actor_name"],
        prompt=d["prompt"],
        status=Status.from_str(d["status"]),
        exit_code=d.get("exit_code"),
        pid=d.get("pid"),
        config=actor_config_from_dict(d.get("config")),
        started_at=d["started_at"],
        finished_at=d.get("finished_at"),
    )


def role_to_dict(role: Role) -> Dict[str, Any]:
    return {
        "name": role.name,
        "agent": role.agent,
        "prompt": role.prompt,
        "description": role.description,
        "config": dict(role.config),
    }


def role_from_dict(d: Dict[str, Any]) -> Role:
    return Role(
        name=d["name"],
        agent=d.get("agent"),
        prompt=d.get("prompt"),
        description=d.get("description"),
        config=dict(d.get("config") or {}),
    )


def log_entry_to_dict(e: LogEntry) -> Dict[str, Any]:
    return {
        "kind": e.kind.value,
        "timestamp": e.timestamp,
        "text": e.text,
        "name": e.name,
        "input": e.input,
        "content": e.content,
        "usage": dict(e.usage) if e.usage is not None else None,
    }


def log_entry_from_dict(d: Dict[str, Any]) -> LogEntry:
    return LogEntry(
        kind=LogEntryKind(d["kind"]),
        timestamp=d.get("timestamp"),
        text=d.get("text", ""),
        name=d.get("name", ""),
        input=d.get("input", ""),
        content=d.get("content", ""),
        usage=dict(d["usage"]) if d.get("usage") is not None else None,
    )


# ---------------------------------------------------------------------------
# Notification serialization
# ---------------------------------------------------------------------------


def notification_to_dict(n: "Notification") -> Dict[str, Any]:  # noqa: F821
    return {
        "actor": n.actor,
        "event": n.event,
        "run_id": n.run_id,
        "status": n.status.value if n.status is not None else None,
        "output": n.output,
        "timestamp": n.timestamp,
    }


def notification_from_dict(d: Dict[str, Any]) -> "Notification":  # noqa: F821
    # Lazy import to avoid a service ↔ protocol cycle.
    from .service import Notification

    status = d.get("status")
    return Notification(
        actor=d["actor"],
        event=d["event"],
        run_id=d.get("run_id"),
        status=Status.from_str(status) if status is not None else None,
        output=d.get("output"),
        timestamp=d.get("timestamp", 0.0),
    )


# ---------------------------------------------------------------------------
# Error mapping (typed exceptions across the wire)
# ---------------------------------------------------------------------------

# Class name → class. Lookup is by `__name__` so subclass renames stay
# contained on the daemon side; the wire format is stable as long as
# the class name is.
_ERROR_REGISTRY: Dict[str, type] = {
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


def error_to_wire(exc: BaseException) -> Tuple[int, str, Dict[str, Any]]:
    """Map an exception to (code, message, data) for a JSON-RPC error.

    All `ActorError` subclasses round-trip with their type name so the
    client can re-raise the same class. Non-`ActorError` exceptions
    surface as INTERNAL_ERROR with no type info.
    """
    if isinstance(exc, ActorError):
        return (
            APPLICATION_ERROR,
            str(exc),
            {"type": type(exc).__name__},
        )
    return (INTERNAL_ERROR, repr(exc), {"type": type(exc).__name__})


def raise_from_wire(code: int, message: str, data: Any) -> None:
    """Inverse of `error_to_wire`. Looks up the registered type by name
    and re-raises with the same message; falls back to `ActorError`.

    Bypasses `__init__` so subclasses with structured constructors
    (e.g. `NotFoundError(name)`) don't double-wrap the message."""
    type_name = None
    if isinstance(data, dict):
        type_name = data.get("type")
    cls = _ERROR_REGISTRY.get(type_name) if type_name else None
    if cls is None:
        # Treat any unrecognized error as ActorError so callers can
        # `except ActorError` and not lose typed error handling.
        raise ActorError(f"daemon error (code={code}): {message}")
    inst = cls.__new__(cls)
    Exception.__init__(inst, message)
    raise inst
