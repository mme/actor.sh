"""actord — daemon process for the actor wire (issue #35).

Runs one `LocalActorService` for the daemon's lifetime and serves
gRPC over a unix socket (`grpclib` + `betterproto`). Phase 2.5 routes
the full ActorService surface, including interactive sessions:
`InteractiveSession` is bidirectional streaming so the daemon can
spawn the agent in a PTY and stream stdout/stderr back to the client
while the client streams stdin / resize / signal forward.

The MCP wire (Claude Code ↔ `actor mcp`) is independent and lives in
`server.py` — `actord` does not import the MCP SDK.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import pty
import signal
import socket as _socket
import struct
import sys
import termios
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Dict, Optional, Set

from grpclib.const import Status as GrpcStatus
from grpclib.exceptions import GRPCError
from grpclib.server import Server, Stream

from . import wire
from ._proto.actor import v1 as pb
from .db import Database
from .errors import ActorError, ConfigError, NotFoundError
from .git import RealGit
from .interfaces import binary_exists
from .process import RealProcessManager
from .service import (
    ActorService,
    INTERACTIVE_PROMPT,
    LocalActorService,
    Notification,
    _app_config_override,
    _caller_actor_name,
    create_agent,
)
from .types import ActorConfig, AgentKind, Status, _now_iso

log = logging.getLogger("actor.daemon")


# ---------------------------------------------------------------------------
# Subscription state
# ---------------------------------------------------------------------------


class _SubscriberRegistry:
    """In-memory set of `asyncio.Queue[Notification]` instances, one
    per active `SubscribeNotifications` stream. Each per-call handler
    pushes onto a fresh queue while it's running and pops itself off
    when the stream ends. The daemon-side `LocalActorService` is
    subscribed once at startup; `fan_out` snapshots the queue list
    and pushes the notification to every subscriber."""

    def __init__(self) -> None:
        self._queues: Set[asyncio.Queue[Notification]] = set()

    def add(self, q: asyncio.Queue[Notification]) -> None:
        self._queues.add(q)

    def remove(self, q: asyncio.Queue[Notification]) -> None:
        self._queues.discard(q)

    async def fan_out(self, n: Notification) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(n)
            except asyncio.QueueFull:
                # Drop on overflow rather than block the publisher.
                # The pre-Phase-2.5 design didn't have a queue depth
                # cap — keep this loud so we notice if it ever fires.
                log.warning("subscriber queue full; dropping notification")


# ---------------------------------------------------------------------------
# Per-call context binding
# ---------------------------------------------------------------------------


class _CallContext:
    """Sets `_app_config_override` + `_caller_actor_name` from the
    incoming gRPC metadata for the duration of the call. Use as a
    context manager — restores the contextvars on exit."""

    def __init__(self, metadata: Optional[dict]) -> None:
        self.metadata = metadata or {}
        self._cfg_token = None
        self._name_token = None

    def __enter__(self) -> "_CallContext":
        cwd = self.metadata.get(wire.META_CALLER_CWD)
        if cwd:
            try:
                from .config import load_config
                cfg = load_config(cwd=Path(cwd))
                self._cfg_token = _app_config_override.set(cfg)
            except Exception as e:
                log.warning(
                    "failed to load AppConfig from caller cwd=%s: %s", cwd, e,
                )
        caller_name = self.metadata.get(wire.META_CALLER_ACTOR_NAME)
        if caller_name:
            self._name_token = _caller_actor_name.set(caller_name)
        return self

    def __exit__(self, *exc) -> None:
        if self._cfg_token is not None:
            _app_config_override.reset(self._cfg_token)
        if self._name_token is not None:
            _caller_actor_name.reset(self._name_token)


def _trailing_error_metadata(exc: BaseException) -> Dict[str, str]:
    """Trailing metadata to attach to a GRPCError so the client can
    recover the original ActorError subclass."""
    return {wire.META_ERROR_TYPE: type(exc).__name__}


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------


class ActorServiceServicer(pb.ActorServiceBase):
    """Glue between gRPC handlers and the in-process
    `LocalActorService`. Each unary handler:

    1. Reads the request off the stream.
    2. Binds caller metadata into contextvars (`_CallContext`).
    3. Awaits `LocalActorService.<method>(...)`.
    4. Sends the matching protobuf response.

    Streaming methods (`subscribe_notifications`, `interactive_session`)
    keep the stream open and drive their own loops.

    `ActorError` subclasses bubble out as `GRPCError` (status code per
    `wire._STATUS_BY_TYPE`) plus a trailing-metadata header naming the
    original class so the client can re-raise the same type.

    We override `__mapping__` from the betterproto-generated
    `ActorServiceBase` so each handler receives the raw `Stream`
    object and can read `stream.metadata`. The default generated
    dispatchers hide the metadata from the user code.
    """

    def __init__(
        self,
        service: LocalActorService,
        subscribers: _SubscriberRegistry,
    ) -> None:
        self._service = service
        self._subs = subscribers

    # -- unary handlers (each receives a raw Stream) ------------------

    async def _new_actor(self, stream: Stream) -> None:
        await _unary(stream, pb.NewActorRequest, self._do_new_actor)

    async def _do_new_actor(self, req: pb.NewActorRequest) -> pb.NewActorResponse:
        actor = await self._service.new_actor(
            name=req.name, dir=req.dir, no_worktree=req.no_worktree,
            base=req.base, agent_name=req.agent_name,
            config=wire.actor_config_from_pb(req.config),
            role_name=req.role_name,
        )
        return pb.NewActorResponse(actor=wire.actor_to_pb(actor))

    async def _discard_actor(self, stream: Stream) -> None:
        await _unary(stream, pb.DiscardActorRequest, self._do_discard_actor)

    async def _do_discard_actor(self, req: pb.DiscardActorRequest) -> pb.DiscardActorResponse:
        result = await self._service.discard_actor(name=req.name, force=req.force)
        return pb.DiscardActorResponse(result=wire.discard_result_to_pb(result))

    async def _config_actor(self, stream: Stream) -> None:
        await _unary(stream, pb.ConfigActorRequest, self._do_config_actor)

    async def _do_config_actor(self, req: pb.ConfigActorRequest) -> pb.ConfigActorResponse:
        cfg = await self._service.config_actor(
            name=req.name,
            pairs=list(req.pairs) if req.pairs else None,
        )
        return pb.ConfigActorResponse(config=wire.actor_config_to_pb(cfg))

    async def _start_run(self, stream: Stream) -> None:
        await _unary(stream, pb.StartRunRequest, self._do_start_run)

    async def _do_start_run(self, req: pb.StartRunRequest) -> pb.StartRunResponse:
        result = await self._service.start_run(
            name=req.name, prompt=req.prompt,
            config=wire.actor_config_from_pb(req.config),
        )
        return pb.StartRunResponse(result=wire.run_start_result_to_pb(result))

    async def _wait_for_run(self, stream: Stream) -> None:
        await _unary(stream, pb.WaitForRunRequest, self._do_wait_for_run)

    async def _do_wait_for_run(self, req: pb.WaitForRunRequest) -> pb.WaitForRunResponse:
        result = await self._service.wait_for_run(req.run_id)
        return pb.WaitForRunResponse(result=wire.run_result_to_pb(result))

    async def _run_actor(self, stream: Stream) -> None:
        await _unary(stream, pb.RunActorRequest, self._do_run_actor)

    async def _do_run_actor(self, req: pb.RunActorRequest) -> pb.RunActorResponse:
        result = await self._service.run_actor(
            name=req.name, prompt=req.prompt,
            config=wire.actor_config_from_pb(req.config),
        )
        return pb.RunActorResponse(result=wire.run_result_to_pb(result))

    async def _stop_actor(self, stream: Stream) -> None:
        await _unary(stream, pb.StopActorRequest, self._do_stop_actor)

    async def _do_stop_actor(self, req: pb.StopActorRequest) -> pb.StopActorResponse:
        result = await self._service.stop_actor(name=req.name)
        return pb.StopActorResponse(result=wire.stop_result_to_pb(result))

    async def _get_actor(self, stream: Stream) -> None:
        await _unary(stream, pb.GetActorRequest, self._do_get_actor)

    async def _do_get_actor(self, req: pb.GetActorRequest) -> pb.GetActorResponse:
        actor = await self._service.get_actor(req.name)
        return pb.GetActorResponse(actor=wire.actor_to_pb(actor))

    async def _actor_exists(self, stream: Stream) -> None:
        await _unary(stream, pb.ActorExistsRequest, self._do_actor_exists)

    async def _do_actor_exists(self, req: pb.ActorExistsRequest) -> pb.ActorExistsResponse:
        exists = await self._service.actor_exists(req.name)
        return pb.ActorExistsResponse(exists=exists)

    async def _list_actors(self, stream: Stream) -> None:
        await _unary(stream, pb.ListActorsRequest, self._do_list_actors)

    async def _do_list_actors(self, req: pb.ListActorsRequest) -> pb.ListActorsResponse:
        actors = await self._service.list_actors(status_filter=req.status_filter)
        return pb.ListActorsResponse(
            actors=[wire.actor_to_pb(a) for a in actors],
        )

    async def _actor_status(self, stream: Stream) -> None:
        await _unary(stream, pb.ActorStatusRequest, self._do_actor_status)

    async def _do_actor_status(self, req: pb.ActorStatusRequest) -> pb.ActorStatusResponse:
        status = await self._service.actor_status(req.name)
        return pb.ActorStatusResponse(status=wire.status_to_pb(status))

    async def _latest_run(self, stream: Stream) -> None:
        await _unary(stream, pb.LatestRunRequest, self._do_latest_run)

    async def _do_latest_run(self, req: pb.LatestRunRequest) -> pb.LatestRunResponse:
        run = await self._service.latest_run(req.actor_name)
        return pb.LatestRunResponse(
            run=wire.run_to_pb(run) if run is not None else None,
        )

    async def _show_actor(self, stream: Stream) -> None:
        await _unary(stream, pb.ShowActorRequest, self._do_show_actor)

    async def _do_show_actor(self, req: pb.ShowActorRequest) -> pb.ShowActorResponse:
        detail = await self._service.show_actor(
            name=req.name, runs_limit=req.runs_limit,
        )
        return pb.ShowActorResponse(detail=wire.actor_detail_to_pb(detail))

    async def _list_runs(self, stream: Stream) -> None:
        await _unary(stream, pb.ListRunsRequest, self._do_list_runs)

    async def _do_list_runs(self, req: pb.ListRunsRequest) -> pb.ListRunsResponse:
        runs, total = await self._service.list_runs(
            actor_name=req.actor_name, limit=req.limit,
        )
        return pb.ListRunsResponse(
            runs=[wire.run_to_pb(r) for r in runs], total=total,
        )

    async def _get_run(self, stream: Stream) -> None:
        await _unary(stream, pb.GetRunRequest, self._do_get_run)

    async def _do_get_run(self, req: pb.GetRunRequest) -> pb.GetRunResponse:
        run = await self._service.get_run(req.run_id)
        return pb.GetRunResponse(
            run=wire.run_to_pb(run) if run is not None else None,
        )

    async def _get_logs(self, stream: Stream) -> None:
        await _unary(stream, pb.GetLogsRequest, self._do_get_logs)

    async def _do_get_logs(self, req: pb.GetLogsRequest) -> pb.GetLogsResponse:
        result = await self._service.get_logs(req.actor_name)
        return pb.GetLogsResponse(result=wire.logs_result_to_pb(result))

    async def _list_roles(self, stream: Stream) -> None:
        await _unary(stream, pb.ListRolesRequest, self._do_list_roles)

    async def _do_list_roles(self, req: pb.ListRolesRequest) -> pb.ListRolesResponse:
        roles = await self._service.list_roles()
        return pb.ListRolesResponse(
            roles={name: wire.role_to_pb(r) for name, r in roles.items()},
        )

    async def _publish_notification(self, stream: Stream) -> None:
        await _unary(stream, pb.PublishNotificationRequest, self._do_publish_notification)

    async def _do_publish_notification(
        self, req: pb.PublishNotificationRequest,
    ) -> pb.PublishNotificationResponse:
        await self._service.publish_notification(
            wire.notification_from_pb(req.notification),
        )
        return pb.PublishNotificationResponse()

    # -- streaming handlers -------------------------------------------

    async def _subscribe_notifications(self, stream: Stream) -> None:
        request = await stream.recv_message()
        assert request is not None
        with _CallContext(stream.metadata):
            queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=256)
            self._subs.add(queue)
            try:
                while True:
                    n = await queue.get()
                    await stream.send_message(wire.notification_to_pb(n))
            except asyncio.CancelledError:
                raise
            finally:
                self._subs.remove(queue)

    async def _interactive_session(self, stream: Stream) -> None:
        with _CallContext(stream.metadata):
            try:
                await _run_interactive(self._service, stream)
            except GRPCError:
                raise
            except ActorError as exc:
                err = wire.actor_error_to_grpc(exc)
                await stream.send_trailing_metadata(
                    status=err.status,
                    status_message=err.message or "",
                    metadata=_trailing_error_metadata(exc),
                )
            except Exception as exc:
                log.exception("interactive_session failed")
                err = wire.actor_error_to_grpc(exc)
                await stream.send_trailing_metadata(
                    status=err.status,
                    status_message=err.message or "",
                    metadata=_trailing_error_metadata(exc),
                )

    # -- mapping override ---------------------------------------------

    def __mapping__(self):
        # Bypass the betterproto-generated request-only handlers; we
        # need the raw `Stream` to read incoming metadata.
        from grpclib import const as _const
        return {
            "/actor.v1.ActorService/NewActor": _const.Handler(
                self._new_actor, _const.Cardinality.UNARY_UNARY,
                pb.NewActorRequest, pb.NewActorResponse,
            ),
            "/actor.v1.ActorService/DiscardActor": _const.Handler(
                self._discard_actor, _const.Cardinality.UNARY_UNARY,
                pb.DiscardActorRequest, pb.DiscardActorResponse,
            ),
            "/actor.v1.ActorService/ConfigActor": _const.Handler(
                self._config_actor, _const.Cardinality.UNARY_UNARY,
                pb.ConfigActorRequest, pb.ConfigActorResponse,
            ),
            "/actor.v1.ActorService/StartRun": _const.Handler(
                self._start_run, _const.Cardinality.UNARY_UNARY,
                pb.StartRunRequest, pb.StartRunResponse,
            ),
            "/actor.v1.ActorService/WaitForRun": _const.Handler(
                self._wait_for_run, _const.Cardinality.UNARY_UNARY,
                pb.WaitForRunRequest, pb.WaitForRunResponse,
            ),
            "/actor.v1.ActorService/RunActor": _const.Handler(
                self._run_actor, _const.Cardinality.UNARY_UNARY,
                pb.RunActorRequest, pb.RunActorResponse,
            ),
            "/actor.v1.ActorService/StopActor": _const.Handler(
                self._stop_actor, _const.Cardinality.UNARY_UNARY,
                pb.StopActorRequest, pb.StopActorResponse,
            ),
            "/actor.v1.ActorService/GetActor": _const.Handler(
                self._get_actor, _const.Cardinality.UNARY_UNARY,
                pb.GetActorRequest, pb.GetActorResponse,
            ),
            "/actor.v1.ActorService/ActorExists": _const.Handler(
                self._actor_exists, _const.Cardinality.UNARY_UNARY,
                pb.ActorExistsRequest, pb.ActorExistsResponse,
            ),
            "/actor.v1.ActorService/ListActors": _const.Handler(
                self._list_actors, _const.Cardinality.UNARY_UNARY,
                pb.ListActorsRequest, pb.ListActorsResponse,
            ),
            "/actor.v1.ActorService/ActorStatus": _const.Handler(
                self._actor_status, _const.Cardinality.UNARY_UNARY,
                pb.ActorStatusRequest, pb.ActorStatusResponse,
            ),
            "/actor.v1.ActorService/LatestRun": _const.Handler(
                self._latest_run, _const.Cardinality.UNARY_UNARY,
                pb.LatestRunRequest, pb.LatestRunResponse,
            ),
            "/actor.v1.ActorService/ShowActor": _const.Handler(
                self._show_actor, _const.Cardinality.UNARY_UNARY,
                pb.ShowActorRequest, pb.ShowActorResponse,
            ),
            "/actor.v1.ActorService/ListRuns": _const.Handler(
                self._list_runs, _const.Cardinality.UNARY_UNARY,
                pb.ListRunsRequest, pb.ListRunsResponse,
            ),
            "/actor.v1.ActorService/GetRun": _const.Handler(
                self._get_run, _const.Cardinality.UNARY_UNARY,
                pb.GetRunRequest, pb.GetRunResponse,
            ),
            "/actor.v1.ActorService/GetLogs": _const.Handler(
                self._get_logs, _const.Cardinality.UNARY_UNARY,
                pb.GetLogsRequest, pb.GetLogsResponse,
            ),
            "/actor.v1.ActorService/ListRoles": _const.Handler(
                self._list_roles, _const.Cardinality.UNARY_UNARY,
                pb.ListRolesRequest, pb.ListRolesResponse,
            ),
            "/actor.v1.ActorService/PublishNotification": _const.Handler(
                self._publish_notification, _const.Cardinality.UNARY_UNARY,
                pb.PublishNotificationRequest, pb.PublishNotificationResponse,
            ),
            "/actor.v1.ActorService/SubscribeNotifications": _const.Handler(
                self._subscribe_notifications, _const.Cardinality.UNARY_STREAM,
                pb.SubscribeNotificationsRequest, pb.Notification,
            ),
            "/actor.v1.ActorService/InteractiveSession": _const.Handler(
                self._interactive_session, _const.Cardinality.STREAM_STREAM,
                pb.ClientFrame, pb.ServerFrame,
            ),
        }


async def _unary(
    stream: Stream,
    request_type: type,
    body: Callable[[Any], Awaitable[Any]],
) -> None:
    """Common boilerplate for the 18 unary methods: read the
    request, bind context, dispatch, translate exceptions to a gRPC
    status + a trailing-metadata header naming the original
    `ActorError` subclass."""
    request = await stream.recv_message()
    assert request is not None
    with _CallContext(stream.metadata):
        try:
            response = await body(request)
            await stream.send_message(response)
        except GRPCError:
            raise
        except ActorError as exc:
            err = wire.actor_error_to_grpc(exc)
            await stream.send_trailing_metadata(
                status=err.status,
                status_message=err.message or "",
                metadata=_trailing_error_metadata(exc),
            )
        except TypeError as exc:
            await stream.send_trailing_metadata(
                status=GrpcStatus.INVALID_ARGUMENT,
                status_message=str(exc),
            )
        except Exception as exc:
            log.exception("unary handler raised")
            err = wire.actor_error_to_grpc(exc)
            await stream.send_trailing_metadata(
                status=err.status,
                status_message=err.message or "",
                metadata=_trailing_error_metadata(exc),
            )


# ---------------------------------------------------------------------------
# Interactive session
# ---------------------------------------------------------------------------


async def _run_interactive(
    service: LocalActorService,
    stream: "Stream",
) -> None:
    """Bidi-streaming InteractiveSession handler.

    Wire shape:
      1. Client sends one ClientFrame{open=OpenSession{actor_name, cols, rows}}.
      2. Daemon spawns the agent in a PTY, streams stdout/stderr as
         ServerFrames, accepts further ClientFrames for stdin / resize
         / signal until the child exits or the client cancels.
      3. Daemon sends a final ServerFrame{exit=ExitInfo{exit_code}} and
         finalizes the run row.

    The PTY master fd lives in this handler; if the client cancels
    (HTTP/2 RST_STREAM), the surrounding `recv_loop` task gets
    cancelled, which flows down to `_finalize_session` — SIGTERM the
    child, close the PTY, mark the run STOPPED.
    """
    # 1. Open
    import betterproto
    first = await stream.recv_message()
    if first is None:
        raise GRPCError(
            GrpcStatus.INVALID_ARGUMENT,
            "client closed before sending OpenSession",
        )
    kind, value = betterproto.which_one_of(first, "kind")
    if kind != "open":
        raise GRPCError(
            GrpcStatus.INVALID_ARGUMENT,
            "first ClientFrame must carry an OpenSession",
        )
    open_msg: pb.OpenSession = value
    actor_name = open_msg.actor_name
    cols = open_msg.cols or 80
    rows = open_msg.rows or 24

    # 2. Reserve the run row + agent argv via the standard service path.
    handle = await service.start_interactive_run(actor_name)

    # 3. Spawn the agent in a PTY.
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows, cols)

    pid = os.fork()
    if pid == 0:  # child
        try:
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                pass
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.close(master_fd)
            env = dict(os.environ)
            env["ACTOR_NAME"] = actor_name
            os.chdir(handle.dir)
            os.execvpe(handle.argv[0], handle.argv, env)
        except Exception:
            os._exit(127)

    os.close(slave_fd)
    await service.update_interactive_run_pid(handle.run_id, pid)

    loop = asyncio.get_running_loop()
    stdout_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=256)

    def _on_master_readable() -> None:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master_fd)
            stdout_q.put_nowait(None)
            return
        stdout_q.put_nowait(data)

    loop.add_reader(master_fd, _on_master_readable)

    async def _writer() -> None:
        """Pull stdout chunks off the queue and forward as ServerFrames."""
        while True:
            chunk = await stdout_q.get()
            if chunk is None:
                return
            await stream.send_message(pb.ServerFrame(stdout=chunk))

    async def _reader() -> None:
        """Read ClientFrames; forward stdin / resize / signal to the
        child until the client closes the stream or sends a signal
        that ends the session."""
        import betterproto
        while True:
            try:
                frame: Optional[pb.ClientFrame] = await stream.recv_message()
            except Exception:
                return
            if frame is None:
                return  # client closed its half
            kind, value = betterproto.which_one_of(frame, "kind")
            if kind == "stdin":
                try:
                    os.write(master_fd, value)
                except OSError:
                    return
            elif kind == "resize":
                _set_winsize(master_fd, value.rows, value.cols)
            elif kind == "signal":
                try:
                    os.kill(pid, value.signal_number)
                except ProcessLookupError:
                    pass

    writer_task = asyncio.create_task(_writer())
    reader_task = asyncio.create_task(_reader())

    exit_code = 0
    try:
        # Reap the child process. asyncio.to_thread keeps the loop
        # responsive while waitpid blocks.
        _, status = await asyncio.to_thread(os.waitpid, pid, 0)
        if os.WIFEXITED(status):
            exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            exit_code = -os.WTERMSIG(status)
    except ChildProcessError:
        exit_code = -1
    finally:
        # Drain remaining stdout, then tear down the readers.
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass
        try:
            tail = os.read(master_fd, 65536)
            if tail:
                stdout_q.put_nowait(tail)
        except OSError:
            pass
        stdout_q.put_nowait(None)
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Drain writer cleanly so all bytes hit the wire before exit.
        try:
            await asyncio.wait_for(writer_task, timeout=5)
        except asyncio.TimeoutError:
            writer_task.cancel()
        if not reader_task.done():
            reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass

    await service.finalize_interactive_run(handle.run_id, exit_code)
    refreshed = await service.get_run(handle.run_id)
    final_status = (
        wire.status_to_pb(refreshed.status)
        if refreshed is not None else pb.Status.DONE
    )
    await stream.send_message(pb.ServerFrame(
        exit=pb.ExitInfo(exit_code=exit_code, final_status=final_status),
    ))


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


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
        return True
    return True


def _check_pidfile(pidfile: Path) -> None:
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
    if not socket_path.exists():
        return
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
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
# Logging
# ---------------------------------------------------------------------------


def _configure_logging(log_file: Optional[Path]) -> None:
    root = logging.getLogger()
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
# Transport URI parsing
# ---------------------------------------------------------------------------


def parse_transport_uri(uri: str) -> "tuple[str, str]":
    """Parse `unix:/path/to/sock` or `unix:~/...sock`.

    `tcp:host:port` raises NotImplementedError until Phase 4 wires up
    the inter-daemon listener.
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
    await service.subscribe_notifications(subscribers.fan_out)

    servicer = ActorServiceServicer(service, subscribers)
    server = Server([servicer])

    await server.start(path=str(socket_path))
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
        description="actord — gRPC daemon for the actor wire",
    )
    parser.add_argument(
        "--listen", default="unix:~/.actor/daemon.sock",
        help="Transport URI (default: unix:~/.actor/daemon.sock)",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Path to actor.db (default: ~/.actor/actor.db)",
    )
    parser.add_argument(
        "--pidfile", default=None,
        help="Path to pidfile (default: ~/.actor/daemon.pid)",
    )
    parser.add_argument(
        "--log-file", default="~/.actor/daemon.log",
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
