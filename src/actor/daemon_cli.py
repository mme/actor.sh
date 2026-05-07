"""Lifecycle commands for `actor daemon ...` (Phase 3).

Five subcommands:
- `start`    — background by default; `--foreground` runs in this
               terminal (used for debugging).
- `stop`     — SIGTERM → wait 10s → SIGKILL. Idempotent.
- `restart`  — `stop` then `start --background`. One invocation.
- `status`   — pid / uptime / version / connection count, ASCII.
- `logs`     — tail `~/.actor/daemon.log`; `-f` streams new lines.

Most clients auto-spawn the daemon, so users rarely run these
explicitly; they're for manual control + debugging.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .bootstrap import (
    daemon_log,
    daemon_pidfile,
    daemon_socket,
    is_pid_alive,
    read_daemon_pid,
)


async def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    cmd = args.daemon_command
    if cmd is None:
        parser.parse_args(["daemon", "--help"])
        return
    if cmd == "start":
        await _cmd_start(args)
    elif cmd == "stop":
        await _cmd_stop(args)
    elif cmd == "restart":
        await _cmd_restart(args)
    elif cmd == "status":
        await _cmd_status(args)
    elif cmd == "logs":
        await _cmd_logs(args)
    else:
        parser.parse_args(["daemon", "--help"])


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


async def _cmd_start(args: argparse.Namespace) -> None:
    """Default: spawn the daemon in a detached process group and
    return once the socket accepts. `--foreground` runs the daemon
    in the current process — Ctrl-C exits, used for debugging."""
    if args.foreground:
        await _start_foreground(args)
        return
    await _start_background(args)


async def _start_foreground(args: argparse.Namespace) -> None:
    from . import daemon
    from .cli import _db_path

    pidfile = daemon_pidfile()
    pid = read_daemon_pid(pidfile)
    if pid is not None and is_pid_alive(pid):
        print(
            f"error: actord already running at PID {pid}; "
            f"use `actor daemon restart` to bounce it",
            file=sys.stderr,
        )
        sys.exit(1)

    log_file = (
        Path(os.path.expanduser(args.log_file)) if args.log_file else None
    )
    try:
        await daemon.main(
            transport_uri=args.listen,
            db_path=_db_path(),
            log_file=log_file,
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


async def _start_background(args: argparse.Namespace) -> None:
    pidfile = daemon_pidfile()
    pid = read_daemon_pid(pidfile)
    if pid is not None and is_pid_alive(pid):
        print(f"actord already running at PID {pid}")
        return

    sock = daemon_socket()
    bin_path = _resolve_actor_bin()
    cmd = bin_path + [
        "daemon", "start", "--foreground",
        "--listen", args.listen,
        "--log-file", args.log_file or "",
    ]
    devnull = subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        stdin=devnull, stdout=devnull, stderr=devnull,
        start_new_session=True,
        close_fds=True,
    )

    # Poll the socket until it accepts, with a clear timeout so we
    # don't hang the user's shell forever if the daemon dies during
    # startup.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            print(
                f"error: actord exited (rc={proc.returncode}) during startup; "
                f"check {daemon_log()}",
                file=sys.stderr,
            )
            sys.exit(1)
        if sock.exists() and _socket_accepts(str(sock)):
            print(f"actord started (PID {proc.pid})")
            return
        await asyncio.sleep(0.05)
    print(
        f"error: actord did not start within 10s; check {daemon_log()}",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


async def _cmd_stop(args: argparse.Namespace) -> None:
    pidfile = daemon_pidfile()
    pid = read_daemon_pid(pidfile)
    if pid is None or not is_pid_alive(pid):
        # Idempotent — silent on a missing pidfile, just clean up
        # any stale leftovers.
        for path in (pidfile, daemon_socket()):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        print("actord not running")
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("actord not running")
        return

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            print(f"actord stopped (was PID {pid})")
            return
        await asyncio.sleep(0.1)

    # Daemon ignored SIGTERM. Escalate.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    print(
        f"actord did not exit on SIGTERM; escalated to SIGKILL (was PID {pid})",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


async def _cmd_restart(args: argparse.Namespace) -> None:
    await _cmd_stop(args)
    # _cmd_start_background needs the start args (listen, log_file).
    # Reconstruct from defaults since `restart` doesn't take them.
    fake_args = argparse.Namespace(
        listen="unix:~/.actor/daemon.sock",
        log_file="~/.actor/daemon.log",
        foreground=False,
    )
    await _start_background(fake_args)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def _cmd_status(args: argparse.Namespace) -> None:
    """Plain ASCII table when running; one-line message when not.
    Exit 0 either way — `not running` is informational, not an error."""
    pidfile = daemon_pidfile()
    sock = daemon_socket()
    pid = read_daemon_pid(pidfile)

    if pid is None or not is_pid_alive(pid):
        print("actord not running")
        return

    # Daemon is up; query it for the rest.
    try:
        from .service import RemoteActorService
        svc = RemoteActorService(f"unix:{sock}", auto_spawn=False)
        try:
            info = await svc.get_server_info()
        finally:
            await svc.aclose()
    except Exception as e:
        # Pid is alive but we can't talk to it — surface what we know
        # rather than blowing up.
        print("actord running")
        print(f"PID:           {pid}")
        print(f"Socket:        {_pretty_path(sock)} (mode 0600)")
        print(f"Status query:  failed ({e})")
        return

    uptime = _format_uptime(info.started_at)
    print("actord running")
    print(f"PID:           {info.pid}")
    print(f"Socket:        {_pretty_path(sock)} (mode 0600)")
    print(f"Version:       {info.version}")
    print(f"Uptime:        {uptime}")
    print(f"Connections:   {info.connection_count}")


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


async def _cmd_logs(args: argparse.Namespace) -> None:
    log = daemon_log()
    if not log.exists():
        print(f"error: {log} does not exist", file=sys.stderr)
        sys.exit(1)

    n = max(0, int(args.lines))
    # Tail the last N lines.
    last_lines = _tail(log, n)
    for line in last_lines:
        print(line, end="" if line.endswith("\n") else "\n")

    if not args.follow:
        return

    # Follow new appends. Use a positional read; if the file shrinks
    # (rotation), reset to the start.
    pos = log.stat().st_size
    try:
        while True:
            await asyncio.sleep(0.5)
            try:
                size = log.stat().st_size
            except FileNotFoundError:
                continue
            if size < pos:
                # Rotated; start over from the new file's beginning.
                pos = 0
            if size == pos:
                continue
            with log.open("rb") as f:
                f.seek(pos)
                chunk = f.read(size - pos).decode("utf-8", errors="replace")
                pos = size
            print(chunk, end="" if chunk.endswith("\n") else "\n")
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_actor_bin() -> list[str]:
    """Locate the `actor` CLI for the spawn. Returns argv-style list."""
    import shutil
    on_path = shutil.which("actor")
    if on_path:
        return [on_path]
    return [sys.executable, "-m", "actor.cli"]


def _socket_accepts(socket_path: str) -> bool:
    import socket as _s
    sock = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        sock.connect(socket_path)
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _pretty_path(p: Path) -> str:
    """Substitute ~ for $HOME so the path reads compact in status."""
    home = os.path.expanduser("~")
    s = str(p)
    if s.startswith(home + "/"):
        return "~/" + s[len(home) + 1:]
    return s


def _format_uptime(started_at: str) -> str:
    """ISO timestamp → '2h 15m' / '5m 12s' / '42s' depending on length."""
    from datetime import datetime, timezone
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "unknown"
    now = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - start).total_seconds()))
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m"
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    return f"{seconds}s"


def _tail(path: Path, n: int) -> list[str]:
    """Return the last `n` lines of `path`. Reads the whole file
    when n is large or the file is small — daemon.log is capped at
    10MB by `RotatingFileHandler`, so a full read is fine."""
    if n <= 0:
        return []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    lines = text.splitlines(keepends=True)
    return lines[-n:]
