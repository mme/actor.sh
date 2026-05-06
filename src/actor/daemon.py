"""actord — daemon process for the actor wire (issue #35).

Runs one `LocalActorService` for the daemon's lifetime and serves
JSON-RPC 2.0 over WebSockets-on-unix-socket. Phase 2 routes the full
ActorService surface (except interactive/PTY paths, which stay
local-only — see service.py and cli.py for the split rationale).

The MCP wire (Claude Code ↔ `actor mcp`) is independent and lives
in `server.py` — `actord` does not import the MCP SDK.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from websockets.asyncio.server import ServerConnection, unix_serve

from .db import Database
from .errors import ActorError, ConfigError
from .git import RealGit
from .process import RealProcessManager
from .protocol import (
    APPLICATION_ERROR,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    ProtocolError,
    actor_config_from_dict,
    actor_to_dict,
    decode_message,
    encode_error,
    encode_notification,
    encode_response,
    error_to_wire,
    log_entry_to_dict,
    notification_to_dict,
    parse_transport_uri,
    role_to_dict,
    run_to_dict,
)
from .service import (
    ActorService,
    LocalActorService,
    Notification,
    create_agent,
)
from .types import ActorConfig

log = logging.getLogger("actor.daemon")


# ---------------------------------------------------------------------------
# Method dispatch
# ---------------------------------------------------------------------------


Dispatcher = Callable[[ActorService, Dict[str, Any]], Awaitable[Any]]


def _opt_str(params: Dict[str, Any], key: str) -> Optional[str]:
    v = params.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise TypeError(f"parameter {key!r} must be a string")
    return v


def _req_str(params: Dict[str, Any], key: str) -> str:
    v = params.get(key)
    if not isinstance(v, str):
        raise TypeError(f"parameter {key!r} must be a string")
    return v


def _req_int(params: Dict[str, Any], key: str) -> int:
    v = params.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise TypeError(f"parameter {key!r} must be an integer")
    return v


def _req_config(params: Dict[str, Any], key: str = "config") -> ActorConfig:
    raw = params.get(key)
    if raw is None:
        return ActorConfig()
    if not isinstance(raw, dict):
        raise TypeError(f"parameter {key!r} must be an object")
    return actor_config_from_dict(raw)


# -- Lifecycle --------------------------------------------------------------


async def _new_actor(service: ActorService, params: Dict[str, Any]) -> Any:
    actor = await service.new_actor(
        name=_req_str(params, "name"),
        dir=_opt_str(params, "dir"),
        no_worktree=bool(params.get("no_worktree", False)),
        base=_opt_str(params, "base"),
        agent_name=_opt_str(params, "agent_name"),
        config=_req_config(params),
        role_name=_opt_str(params, "role_name"),
    )
    return actor_to_dict(actor)


async def _discard_actor(service: ActorService, params: Dict[str, Any]) -> Any:
    result = await service.discard_actor(
        name=_req_str(params, "name"),
        force=bool(params.get("force", False)),
    )
    return {"names": list(result.names)}


async def _config_actor(service: ActorService, params: Dict[str, Any]) -> Any:
    pairs = params.get("pairs")
    if pairs is not None and not isinstance(pairs, list):
        raise TypeError("parameter 'pairs' must be a list of strings")
    cfg = await service.config_actor(
        name=_req_str(params, "name"),
        pairs=list(pairs) if pairs else None,
    )
    return {
        "actor_keys": dict(cfg.actor_keys),
        "agent_args": dict(cfg.agent_args),
    }


# -- Run lifecycle ----------------------------------------------------------


async def _start_run(service: ActorService, params: Dict[str, Any]) -> Any:
    handle = await service.start_run(
        name=_req_str(params, "name"),
        prompt=_req_str(params, "prompt"),
        config=_req_config(params),
    )
    return {
        "run_id": handle.run_id,
        "pid": handle.pid,
        "status": handle.status.value,
    }


async def _wait_for_run(service: ActorService, params: Dict[str, Any]) -> Any:
    result = await service.wait_for_run(_req_int(params, "run_id"))
    return {
        "run_id": result.run_id,
        "actor": result.actor,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "output": result.output,
    }


async def _run_actor(service: ActorService, params: Dict[str, Any]) -> Any:
    result = await service.run_actor(
        name=_req_str(params, "name"),
        prompt=_req_str(params, "prompt"),
        config=_req_config(params),
    )
    return {
        "run_id": result.run_id,
        "actor": result.actor,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "output": result.output,
    }


async def _stop_actor(service: ActorService, params: Dict[str, Any]) -> Any:
    result = await service.stop_actor(name=_req_str(params, "name"))
    return {"name": result.name, "was_alive": result.was_alive}


# -- Discovery --------------------------------------------------------------


async def _get_actor(service: ActorService, params: Dict[str, Any]) -> Any:
    return actor_to_dict(await service.get_actor(_req_str(params, "name")))


async def _actor_exists(service: ActorService, params: Dict[str, Any]) -> Any:
    return await service.actor_exists(_req_str(params, "name"))


async def _list_actors(service: ActorService, params: Dict[str, Any]) -> Any:
    actors = await service.list_actors(status_filter=_opt_str(params, "status_filter"))
    return [actor_to_dict(a) for a in actors]


async def _actor_status(service: ActorService, params: Dict[str, Any]) -> Any:
    status = await service.actor_status(_req_str(params, "name"))
    return status.value


async def _latest_run(service: ActorService, params: Dict[str, Any]) -> Any:
    run = await service.latest_run(_req_str(params, "actor_name"))
    return run_to_dict(run) if run is not None else None


async def _show_actor(service: ActorService, params: Dict[str, Any]) -> Any:
    runs_limit = params.get("runs_limit", 5)
    if not isinstance(runs_limit, int) or isinstance(runs_limit, bool):
        raise TypeError("parameter 'runs_limit' must be an integer")
    detail = await service.show_actor(
        name=_req_str(params, "name"),
        runs_limit=runs_limit,
    )
    return {
        "actor": actor_to_dict(detail.actor),
        "status": detail.status.value,
        "runs": [run_to_dict(r) for r in detail.runs],
        "total_runs": detail.total_runs,
        "runs_limit": detail.runs_limit,
    }


async def _list_runs(service: ActorService, params: Dict[str, Any]) -> Any:
    runs, total = await service.list_runs(
        actor_name=_req_str(params, "actor_name"),
        limit=_req_int(params, "limit"),
    )
    return {
        "runs": [run_to_dict(r) for r in runs],
        "total": total,
    }


async def _get_run(service: ActorService, params: Dict[str, Any]) -> Any:
    run = await service.get_run(_req_int(params, "run_id"))
    return run_to_dict(run) if run is not None else None


async def _get_logs(service: ActorService, params: Dict[str, Any]) -> Any:
    result = await service.get_logs(_req_str(params, "actor_name"))
    return {
        "session_id": result.session_id,
        "entries": [log_entry_to_dict(e) for e in result.entries],
    }


async def _list_roles(service: ActorService, params: Dict[str, Any]) -> Any:
    roles = await service.list_roles()
    return {name: role_to_dict(role) for name, role in roles.items()}


# -- Notifications ----------------------------------------------------------


async def _publish_notification(service: ActorService, params: Dict[str, Any]) -> Any:
    raw = params.get("notification")
    if not isinstance(raw, dict):
        raise TypeError("parameter 'notification' must be an object")
    from .protocol import notification_from_dict
    await service.publish_notification(notification_from_dict(raw))
    return None


# `subscribe_notifications` is the one method that does NOT go through
# this table — it needs the raw connection to push notifications back.
# The handler intercepts it; see _handle below.
METHODS: Dict[str, Dispatcher] = {
    "new_actor": _new_actor,
    "discard_actor": _discard_actor,
    "config_actor": _config_actor,
    "start_run": _start_run,
    "wait_for_run": _wait_for_run,
    "run_actor": _run_actor,
    "stop_actor": _stop_actor,
    "get_actor": _get_actor,
    "actor_exists": _actor_exists,
    "list_actors": _list_actors,
    "actor_status": _actor_status,
    "latest_run": _latest_run,
    "show_actor": _show_actor,
    "list_runs": _list_runs,
    "get_run": _get_run,
    "get_logs": _get_logs,
    "list_roles": _list_roles,
    "publish_notification": _publish_notification,
}

# Subscription is method-name-only here; handled inline.
SUBSCRIBE_METHOD = "subscribe_notifications"
NOTIFICATION_METHOD = "channel.notification"


# ---------------------------------------------------------------------------
# Subscription state
# ---------------------------------------------------------------------------


class _SubscriberRegistry:
    """Per-daemon set of WebSocket connections that have asked to
    receive `Notification` events. Each connection registers exactly
    once via the `subscribe_notifications` request; closing the
    connection (or sending the same method a second time as an
    unsubscribe — rejected for now) removes it.

    Notifications are sent as JSON-RPC notifications (no `id`) on the
    method `channel.notification` with the serialized `Notification`
    dict in `params`.
    """

    def __init__(self) -> None:
        self._subscribers: Set[ServerConnection] = set()

    def add(self, ws: ServerConnection) -> None:
        self._subscribers.add(ws)

    def remove(self, ws: ServerConnection) -> None:
        self._subscribers.discard(ws)

    async def fan_out(self, n: Notification) -> None:
        if not self._subscribers:
            return
        frame = encode_notification(JSONRPCNotification(
            method=NOTIFICATION_METHOD,
            params=notification_to_dict(n),
        ))
        # Snapshot — a slow subscriber that fails mid-send shouldn't
        # block the others.
        dead: list[ServerConnection] = []
        for ws in list(self._subscribers):
            try:
                await ws.send(frame)
            except Exception as e:
                log.warning("subscriber send failed; dropping: %s", e)
                dead.append(ws)
        for ws in dead:
            self._subscribers.discard(ws)


# ---------------------------------------------------------------------------
# Pidfile + stale-socket handling
# ---------------------------------------------------------------------------


def _read_pid(pidfile: Path) -> Optional[int]:
    try:
        text = pidfile.read_text().strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists; we just can't signal it. Treat as alive.
        return True
    return True


def _check_pidfile(pidfile: Path) -> None:
    """Inspect an existing pidfile. Raise if a live daemon owns it;
    unlink + log if it's stale."""
    if not pidfile.exists():
        return
    pid = _read_pid(pidfile)
    if pid is not None and _pid_alive(pid):
        raise RuntimeError(f"daemon already running at PID {pid}")
    log.warning("removing stale pidfile %s (pid=%s not alive)", pidfile, pid)
    try:
        pidfile.unlink()
    except FileNotFoundError:
        pass


def _check_socket(socket_path: Path) -> None:
    """If a unix socket file exists at the path, probe it. Live
    listener → abort. Refused connection → unlink and continue."""
    if not socket_path.exists():
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        sock.connect(str(socket_path))
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        log.warning("removing stale socket %s", socket_path)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        return
    finally:
        sock.close()
    raise RuntimeError(f"daemon already running on socket {socket_path}")


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


def _make_handler(
    service: ActorService,
    subscribers: _SubscriberRegistry,
) -> Callable[[ServerConnection], Awaitable[None]]:
    async def handler(websocket: ServerConnection) -> None:
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    msg = decode_message(raw)
                except ProtocolError as e:
                    await websocket.send(encode_error(JSONRPCError(
                        id=None, code=INVALID_REQUEST, message=str(e),
                    )))
                    continue

                if not isinstance(msg, JSONRPCRequest):
                    # Phase 2 ignores client-sent responses /
                    # notifications / errors.
                    continue

                params = msg.params or {}

                if msg.method == SUBSCRIBE_METHOD:
                    subscribers.add(websocket)
                    await websocket.send(encode_response(JSONRPCResponse(
                        id=msg.id, result={"subscribed": True},
                    )))
                    continue

                fn = METHODS.get(msg.method)
                if fn is None:
                    await websocket.send(encode_error(JSONRPCError(
                        id=msg.id, code=METHOD_NOT_FOUND,
                        message=f"method not found: {msg.method}",
                    )))
                    continue

                try:
                    result = await fn(service, params)
                    await websocket.send(encode_response(JSONRPCResponse(
                        id=msg.id, result=result,
                    )))
                except TypeError as e:
                    await websocket.send(encode_error(JSONRPCError(
                        id=msg.id, code=INVALID_PARAMS, message=str(e),
                    )))
                except KeyError as e:
                    await websocket.send(encode_error(JSONRPCError(
                        id=msg.id, code=INVALID_PARAMS,
                        message=f"missing parameter: {e}",
                    )))
                except ActorError as exc:
                    code, message, data = error_to_wire(exc)
                    await websocket.send(encode_error(JSONRPCError(
                        id=msg.id, code=code, message=message, data=data,
                    )))
                except Exception as exc:
                    log.exception("dispatch error for %s", msg.method)
                    code, message, data = error_to_wire(exc)
                    await websocket.send(encode_error(JSONRPCError(
                        id=msg.id, code=code, message=message, data=data,
                    )))
        finally:
            subscribers.remove(websocket)

    return handler


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging(log_file: Optional[Path]) -> None:
    root = logging.getLogger()
    # Idempotent across re-entries (tests may import + re-run).
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------


def _build_service(db_path: str) -> LocalActorService:
    db = Database.open(db_path)
    try:
        from .config import load_config
        app_config = load_config()
    except ConfigError as e:
        log.warning("settings.kdl ignored (%s); proceeding with defaults", e)
        app_config = None
    return LocalActorService(
        db=db,
        git=RealGit(),
        proc_mgr=RealProcessManager(),
        agent_factory=create_agent,
        app_config=app_config,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def main(
    transport_uri: str,
    db_path: str,
    log_file: Optional[Path] = None,
    pidfile: Optional[Path] = None,
) -> None:
    _configure_logging(log_file)

    scheme, target = parse_transport_uri(transport_uri)
    if scheme != "unix":
        raise NotImplementedError(
            f"transport {scheme!r} not yet supported; see #35 phase 4"
        )
    socket_path = Path(target)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    if pidfile is None:
        pidfile = Path(os.path.expanduser("~/.actor/daemon.pid"))
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    _check_pidfile(pidfile)
    _check_socket(socket_path)

    service = _build_service(db_path)
    subscribers = _SubscriberRegistry()

    # Bridge service-side notifications to subscriber connections.
    await service.subscribe_notifications(subscribers.fan_out)

    handler = _make_handler(service, subscribers)

    server = await unix_serve(handler, str(socket_path))
    # Tighten permissions to owner-only — the kernel is our auth.
    try:
        os.chmod(socket_path, 0o600)
    except OSError as e:
        log.warning("could not chmod %s to 0600: %s", socket_path, e)

    pidfile.write_text(f"{os.getpid()}\n")
    log.info(
        "actord listening on %s (db=%s, pidfile=%s)",
        socket_path, db_path, pidfile,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signame: str) -> None:
        log.info("received %s, shutting down", signame)
        stop_event.set()

    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        try:
            loop.add_signal_handler(sig, _request_stop, name)
        except NotImplementedError:
            # Some platforms (e.g. Windows) don't support add_signal_handler.
            signal.signal(sig, lambda *_args, n=name: _request_stop(n))

    try:
        await stop_event.wait()
    finally:
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("server.wait_closed() timed out; forcing exit")
        for path in (pidfile, socket_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("could not unlink %s: %s", path, e)


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="actor.daemon",
        description="actord — JSON-RPC daemon for the actor wire",
    )
    parser.add_argument(
        "--listen",
        default="unix:~/.actor/daemon.sock",
        help="Transport URI (default: unix:~/.actor/daemon.sock)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to actor.db (default: ~/.actor/actor.db)",
    )
    parser.add_argument(
        "--pidfile",
        default=None,
        help="Path to pidfile (default: ~/.actor/daemon.pid)",
    )
    parser.add_argument(
        "--log-file",
        default="~/.actor/daemon.log",
        help="Path to log file (default: ~/.actor/daemon.log; pass empty to disable)",
    )
    return parser


def _resolve_db_path(arg: Optional[str]) -> str:
    if arg is not None:
        return os.path.expanduser(arg)
    home = os.environ.get("HOME", "")
    if not home:
        raise ActorError("HOME environment variable is not set")
    return f"{home}/.actor/actor.db"


def _module_main(argv: Optional[list[str]] = None) -> None:
    args = _build_argparser().parse_args(argv)
    log_file = (
        Path(os.path.expanduser(args.log_file))
        if args.log_file else None
    )
    pidfile = (
        Path(os.path.expanduser(args.pidfile))
        if args.pidfile else None
    )
    try:
        asyncio.run(main(
            transport_uri=args.listen,
            db_path=_resolve_db_path(args.db_path),
            log_file=log_file,
            pidfile=pidfile,
        ))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _module_main()
