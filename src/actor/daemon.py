"""actord — daemon process for the actor wire (issue #35).

Runs one `LocalActorService` for the daemon's lifetime and serves
JSON-RPC 2.0 over WebSockets-on-unix-socket. Phase 1 carries one
method (`list_actors`); Phase 2 fills the rest.

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
from typing import Any, Awaitable, Callable, Dict, Optional

from websockets.asyncio.server import ServerConnection, unix_serve

from .db import Database
from .errors import ActorError, ConfigError, NotFoundError
from .git import RealGit
from .process import RealProcessManager
from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    ProtocolError,
    actor_to_dict,
    decode_message,
    encode_error,
    encode_response,
    parse_transport_uri,
)
from .service import ActorService, LocalActorService, create_agent

log = logging.getLogger("actor.daemon")


# Method dispatch table: keep additions in this dict so Phase 2 can
# extend coverage by adding entries here rather than touching the
# connection-handler control flow.
Dispatcher = Callable[[ActorService, Dict[str, Any]], Awaitable[Any]]


async def _list_actors(service: ActorService, params: Dict[str, Any]) -> Any:
    actors = await service.list_actors(status_filter=params.get("status_filter"))
    return [actor_to_dict(a) for a in actors]


METHODS: Dict[str, Dispatcher] = {
    "list_actors": _list_actors,
}


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


def _make_handler(service: ActorService) -> Callable[[ServerConnection], Awaitable[None]]:
    async def handler(websocket: ServerConnection) -> None:
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
                # Phase 1 ignores client-sent responses/errors.
                continue

            try:
                result = await _dispatch(service, msg.method, msg.params or {})
                await websocket.send(encode_response(JSONRPCResponse(
                    id=msg.id, result=result,
                )))
            except KeyError:
                await websocket.send(encode_error(JSONRPCError(
                    id=msg.id, code=METHOD_NOT_FOUND,
                    message=f"method not found: {msg.method}",
                )))
            except TypeError as e:
                await websocket.send(encode_error(JSONRPCError(
                    id=msg.id, code=INVALID_PARAMS, message=str(e),
                )))
            except NotFoundError as e:
                # Application-level "not found" — surface as INVALID_PARAMS
                # so clients can distinguish wire errors from method-not-found.
                await websocket.send(encode_error(JSONRPCError(
                    id=msg.id, code=INVALID_PARAMS, message=str(e),
                )))
            except ActorError as e:
                await websocket.send(encode_error(JSONRPCError(
                    id=msg.id, code=INTERNAL_ERROR, message=str(e),
                )))
            except Exception as e:
                log.exception("dispatch error for %s", msg.method)
                await websocket.send(encode_error(JSONRPCError(
                    id=msg.id, code=INTERNAL_ERROR, message=repr(e),
                )))

    return handler


async def _dispatch(service: ActorService, method: str, params: Dict[str, Any]) -> Any:
    fn = METHODS.get(method)
    if fn is None:
        raise KeyError(method)
    return await fn(service, params)


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
            f"transport {scheme!r} not yet supported; see #35 phase 2"
        )
    socket_path = Path(target)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    if pidfile is None:
        pidfile = Path(os.path.expanduser("~/.actor/daemon.pid"))
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    _check_pidfile(pidfile)
    _check_socket(socket_path)

    service = _build_service(db_path)
    handler = _make_handler(service)

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
