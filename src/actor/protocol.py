"""JSON-RPC 2.0 framing for the actord wire (issue #35).

This is wire 2 — clients (CLI, future MCP bridge, watch) ↔ `actord`.
Pure stdlib JSON; no MCP SDK on this wire. Wire 1 (Claude Code ↔
`actor mcp`, stdio MCP) lives in `server.py` and is untouched.

Phase 1 carries one method (`list_actors`); Phase 2 fills the rest.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from .types import Actor, ActorConfig, AgentKind


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 standard error codes
# ---------------------------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


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
class JSONRPCError:
    id: Optional[Union[int, str]]
    code: int
    message: str
    data: Any = None


Message = Union[JSONRPCRequest, JSONRPCResponse, JSONRPCError]


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

    Discriminates request / response / error by which top-level keys
    are present. Raises `ProtocolError` for malformed input."""
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
        msg_id = obj.get("id")
        params = obj.get("params")
        if params is not None and not isinstance(params, dict):
            raise ProtocolError("'params' must be an object (positional params not supported)")
        return JSONRPCRequest(id=msg_id, method=method, params=params)

    if "error" in obj:
        err = obj["error"]
        if not isinstance(err, dict):
            raise ProtocolError("'error' must be an object")
        code = err.get("code")
        message = err.get("message")
        if not isinstance(code, int) or not isinstance(message, str):
            raise ProtocolError("'error' must have integer 'code' and string 'message'")
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


def parse_transport_uri(uri: str) -> tuple[str, str]:
    """Parse a transport URI into (scheme, target).

    Supported:
        unix:/path/to/sock      → ("unix", "/path/to/sock")
        unix:~/.actor/sock      → ("unix", "/home/.../.actor/sock")
        tcp:host:port           → raises NotImplementedError (Phase 2)

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
            f"see issue #35 phase 2"
        )
    raise ValueError(f"unknown transport scheme: {scheme!r}")


# ---------------------------------------------------------------------------
# Actor (de)serialization
# ---------------------------------------------------------------------------


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
        "config": {
            "actor_keys": dict(actor.config.actor_keys),
            "agent_args": dict(actor.config.agent_args),
        },
        "created_at": actor.created_at,
        "updated_at": actor.updated_at,
    }


def actor_from_dict(d: Dict[str, Any]) -> Actor:
    config_obj = d.get("config") or {}
    return Actor(
        name=d["name"],
        agent=AgentKind.from_str(d["agent"]),
        agent_session=d.get("agent_session"),
        dir=d["dir"],
        source_repo=d.get("source_repo"),
        base_branch=d.get("base_branch"),
        worktree=bool(d["worktree"]),
        parent=d.get("parent"),
        config=ActorConfig(
            actor_keys=dict(config_obj.get("actor_keys") or {}),
            agent_args=dict(config_obj.get("agent_args") or {}),
        ),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )
