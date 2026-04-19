"""Interactive PTY session: fork + execvpe, non-blocking read, resize, kill.

One PtySession owns:
  - a forked child process
  - the master PTY file descriptor
  - the on_output callback that receives bytes as they arrive
  - the on_exit callback that fires when the child exits

No Textual / rendering concerns live here — that's widget.py's job.
asyncio-based: add_reader schedules the read callback on the event loop.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import termios
from pathlib import Path
from typing import Callable, Dict, List, Optional


# Observed "best" chunk size that keeps latency low without overwhelming
# the parser. 64 KiB matches cmdlane; keeps us aligned with how OS delivers.
_READ_CHUNK = 65536


class PtySession:
    """Manages one forked-pty child process."""

    def __init__(
        self,
        argv: List[str],
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        rows: int = 24,
        cols: int = 80,
        on_output: Optional[Callable[[bytes], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
    ) -> None:
        if not argv:
            raise ValueError("argv must be non-empty")
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._rows = rows
        self._cols = cols
        self._on_output = on_output
        self._on_exit = on_exit
        self._pid: Optional[int] = None
        self._fd: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._exit_code: Optional[int] = None
        self._exit_fired = False

    # -- lifecycle ---------------------------------------------------------

    def spawn(self) -> None:
        """Fork + exec the child. Must be called from an async context so
        the running loop can be used as the read-loop driver."""
        if self._pid is not None:
            raise RuntimeError("already spawned")

        pid, fd = pty.fork()
        if pid == 0:
            # Child: replace process image.
            try:
                _set_winsize(0, self._rows, self._cols)  # 0 = our stdin, now the slave
                if self._env is None:
                    env = dict(os.environ)
                else:
                    env = dict(self._env)
                # Ensure the child sees a sane TERM — claude uses it for color.
                env.setdefault("TERM", "xterm-256color")
                os.chdir(str(self._cwd))
                os.execvpe(self._argv[0], self._argv, env)
            except BaseException:
                # If exec fails, don't come back into asyncio-land.
                os._exit(127)

        self._pid = pid
        self._fd = fd
        _set_nonblocking(fd)
        _set_winsize(fd, self._rows, self._cols)

        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(fd, self._on_fd_ready)

    # -- I/O ---------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Write input bytes to the child. Silently no-ops if the session
        has already exited (caller can check `.exited` if they care)."""
        if self._fd is None:
            return
        try:
            os.write(self._fd, data)
        except OSError as e:
            if e.errno in (errno.EBADF, errno.EIO):
                self._handle_exit()
                return
            raise

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        self._rows = rows
        self._cols = cols
        if self._fd is not None:
            try:
                _set_winsize(self._fd, rows, cols)
            except OSError:
                pass

    def close(self, signal_no: int = signal.SIGTERM) -> None:
        """Kill + reap the child. Idempotent. Fires on_exit exactly once."""
        if self._pid is None and self._exit_fired:
            return
        if self._pid is not None:
            try:
                os.kill(self._pid, signal_no)
            except ProcessLookupError:
                pass
        self._handle_exit()

    def kill(self) -> None:
        """Unconditionally SIGKILL the child."""
        self.close(signal.SIGKILL)

    # -- properties --------------------------------------------------------

    @property
    def pid(self) -> Optional[int]:
        return self._pid

    @property
    def fd(self) -> Optional[int]:
        return self._fd

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def exited(self) -> bool:
        return self._exit_code is not None

    # -- internals ---------------------------------------------------------

    def _on_fd_ready(self) -> None:
        if self._fd is None:
            return
        try:
            data = os.read(self._fd, _READ_CHUNK)
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                # PTY closed (child exited / slave released).
                self._handle_exit()
                return
            if e.errno == errno.EAGAIN:
                return
            raise
        if not data:
            self._handle_exit()
            return
        if self._on_output is not None:
            self._on_output(data)

    def _handle_exit(self) -> None:
        if self._exit_fired:
            return
        self._exit_fired = True
        if self._fd is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._fd)
            except (ValueError, KeyError):
                pass
        self._reap()
        if self._on_exit is not None and self._exit_code is not None:
            self._on_exit(self._exit_code)

    def _reap(self) -> None:
        if self._pid is None:
            return
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
            if pid == 0:
                # Still alive; try a blocking wait briefly.
                pid, status = os.waitpid(self._pid, 0)
        except ChildProcessError:
            # Already reaped.
            self._exit_code = self._exit_code if self._exit_code is not None else -1
            self._pid = None
            return
        # Translate status to exit code.
        if os.WIFEXITED(status):
            self._exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self._exit_code = -os.WTERMSIG(status)
        else:
            self._exit_code = -1
        self._pid = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


# --- low-level helpers ----------------------------------------------------

def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """TIOCSWINSZ: rows, cols, x-pixels, y-pixels."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
