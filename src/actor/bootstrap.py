"""Auto-spawn `actord` for clients that need it (Phase 3).

`RemoteActorService.__init__` and the lifecycle CLI both call
`ensure_daemon_running(socket_path)`. If the daemon is already up,
we return immediately. Otherwise we Popen `actor daemon start
--foreground` in a detached process group, then poll the socket
until it accepts.

Two callers, two voices:
- CLI users: `quiet=False` writes one stderr line ("starting
  actord…") so they know what's happening.
- MCP bridge: `quiet=True` — Claude Code surfaces our stderr in the
  chat, and an unsolicited "starting actord" line every cold session
  is noise.
"""
from __future__ import annotations

import asyncio
import os
import socket as _socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def _socket_accepts(socket_path: str, timeout: float = 0.2) -> bool:
    """Cheap probe: connect to the unix socket. The kernel binds the
    socket as part of the daemon's `Server.start()`, so a successful
    connect means the listener is ready. The actual gRPC handshake
    happens lazily on first RPC."""
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
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


def _resolve_actor_bin() -> str:
    """Locate the `actor` CLI for the spawn. Prefer the one on PATH
    (matches what the user sees); fall back to `python -m actor.cli`
    so dev installs without the wheel still work."""
    import shutil
    on_path = shutil.which("actor")
    if on_path:
        return on_path
    return f"{sys.executable} -m actor.cli"


async def ensure_daemon_running(
    socket_path: str,
    *,
    quiet: bool = False,
    timeout: float = 5.0,
) -> None:
    """Make sure `actord` is up. Returns when the socket accepts;
    raises `DaemonUnreachableError` if the daemon doesn't come up
    within `timeout` seconds.

    Race-safe: if two clients try to spawn concurrently, one wins on
    the pidfile + socket-bind dance and the other's Popen exits with
    "daemon already running"; we ignore that and keep polling.
    """
    from .errors import DaemonUnreachableError

    if _socket_accepts(socket_path):
        return

    if not quiet:
        print("starting actord…", file=sys.stderr, flush=True)

    actor_bin = _resolve_actor_bin()
    args = actor_bin.split() + ["daemon", "start", "--foreground"]
    # Detach so the daemon outlives this client process. devnull
    # stdio so it doesn't inherit the caller's tty.
    devnull = subprocess.DEVNULL
    try:
        subprocess.Popen(
            args,
            stdin=devnull, stdout=devnull, stderr=devnull,
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError as e:
        raise DaemonUnreachableError(socket_path, e) from e

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _socket_accepts(socket_path):
            return
        await asyncio.sleep(0.05)
    raise DaemonUnreachableError(
        socket_path,
        RuntimeError(f"actord did not bind {socket_path} within {timeout}s"),
    )


# ---------------------------------------------------------------------------
# Lifecycle helpers shared by `actor daemon {start,stop,restart,status,logs}`
# ---------------------------------------------------------------------------


def daemon_pidfile() -> Path:
    return Path(os.path.expanduser("~/.actor/daemon.pid"))


def daemon_socket() -> Path:
    return Path(os.path.expanduser("~/.actor/daemon.sock"))


def daemon_log() -> Path:
    return Path(os.path.expanduser("~/.actor/daemon.log"))


def read_daemon_pid(pidfile: Optional[Path] = None) -> Optional[int]:
    """Read the recorded PID from the pidfile, or None if missing /
    malformed. The PID may not be alive — callers verify with
    `is_pid_alive`."""
    pf = pidfile or daemon_pidfile()
    try:
        text = pf.read_text().strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
