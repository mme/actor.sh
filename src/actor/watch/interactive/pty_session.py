"""Interactive PTY session lifetime (fork + execvpe, async read, resize, kill).

Callback-based: on_output gets each chunk as it arrives; on_exit fires
once when the child is reaped. Textual / rendering concerns live in
widget.py.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import re
import signal
import struct
import termios
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .diagnostics import DiagnosticRecorder, EventKind


# Terminal-capability queries emitted by modern TUIs on startup. A real
# terminal answers these by writing back to the program's stdin; our pty
# wrapper has no terminal layer, so without auto-replies the program
# blocks waiting for the answer and falls back to degraded rendering
# (or, in codex's case, never paints its input box at all).
#
# Patterns and canned replies — all targeted at "xterm-256color" since
# that's what we set TERM to in the child env. The replies are written
# back to the pty master, which the child reads as ordinary stdin
# input (same path a keypress would take).
_QUERY_REPLIES: list[tuple[re.Pattern[bytes], bytes]] = [
    # CSI 6 n — DSR cursor position. Real terminals reply with the
    # current row/col; for our purposes "1;1" is fine — TUIs use this
    # as a liveness check, not to position content.
    (re.compile(rb"\x1b\[6n"), b"\x1b[1;1R"),
    # CSI 5 n — DSR device status (terminal OK).
    (re.compile(rb"\x1b\[5n"), b"\x1b[0n"),
    # CSI c / CSI 0 c — DA1 primary device attributes. xterm-256color
    # advertises: 64 (VT420), plus modules 1 (132-col), 2 (printer),
    # 6 (selective erase), 9 (national replacement), 15 (DEC tech),
    # 22 (ANSI color).
    (re.compile(rb"\x1b\[c"), b"\x1b[?64;1;2;6;9;15;22c"),
    (re.compile(rb"\x1b\[0c"), b"\x1b[?64;1;2;6;9;15;22c"),
    # CSI ? u — kitty keyboard protocol query. Reply 0 = no flags
    # supported, which keeps codex on its plain-CSI keymap.
    (re.compile(rb"\x1b\[\?u"), b"\x1b[?0u"),
    # OSC 10 ? ST — foreground color query. Reply with a neutral
    # light-gray; codex uses this to compute contrast for its own
    # palette but the exact value isn't load-bearing.
    (
        re.compile(rb"\x1b\]10;\?(\x1b\\|\x07)"),
        b"\x1b]10;rgb:cccc/cccc/cccc\x1b\\",
    ),
    # OSC 11 ? ST — background color query. Reply dark; same caveat.
    (
        re.compile(rb"\x1b\]11;\?(\x1b\\|\x07)"),
        b"\x1b]11;rgb:1111/1414/1c1c\x1b\\",
    ),
]


def _build_query_replies(data: bytes) -> bytes:
    """Scan `data` for terminal-capability queries and concatenate the
    canned replies for every match. Returns `b""` when nothing matched
    so a fast no-write path is possible. Idempotent: each pattern is
    matched independently so a chunk carrying e.g. one DSR + one DA1
    produces both replies in one buffer."""
    out = bytearray()
    for pattern, reply in _QUERY_REPLIES:
        for _ in pattern.finditer(data):
            out.extend(reply)
    return bytes(out)


# 64 KiB chunk keeps latency low without overwhelming the parser.
_READ_CHUNK = 65536
# Liveness-probe poll for close()'s kill(pid, 0) loop before SIGKILL escalation.
_TERM_POLL_S = 0.05
_TERM_DEADLINE_S = 0.5
# Short poll used by _reap() when the child hasn't quite exited yet but we
# also don't want to block the asyncio loop indefinitely on a natural-EOF
# path (EIO/EBADF from os.read can arrive before the child is reaped).
_REAP_POLL_S = 0.02
_REAP_DEADLINE_S = 0.5


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
        recorder: Optional[DiagnosticRecorder] = None,
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
        self._recorder = recorder
        self._pid: Optional[int] = None
        self._fd: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._exit_code: Optional[int] = None
        self._exit_fired = False
        # Pending writes held back by EAGAIN, drained via add_writer.
        self._write_queue: bytearray = bytearray()
        self._writer_registered = False

    def set_callbacks(
        self,
        *,
        on_output: Optional[Callable[[bytes], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Swap callbacks. Used by widget + manager to chain handlers.
        Pass only the fields you want to replace — omitted args stay."""
        if on_output is not None:
            self._on_output = on_output
        if on_exit is not None:
            self._on_exit = on_exit

    # -- lifecycle ---------------------------------------------------------

    def spawn(self) -> None:
        if self._pid is not None:
            raise RuntimeError("already spawned")

        pid, fd = pty.fork()
        if pid == 0:
            # In the child, fds 0/1/2 are already the pty slave after pty.fork.
            try:
                _set_winsize(0, self._rows, self._cols)
                env = dict(os.environ) if self._env is None else dict(self._env)
                env.setdefault("TERM", "xterm-256color")
                os.chdir(str(self._cwd))
                os.execvpe(self._argv[0], self._argv, env)
            except BaseException as e:
                # Can't raise back out of the forked child — its parent
                # already forked away. Surface an exec-failure hint on
                # stderr so the user sees "exit 127" with a reason.
                try:
                    os.write(2, f"actor: exec failed: {e}\n".encode())
                except Exception:
                    pass
                os._exit(127)

        self._pid = pid
        self._fd = fd
        _set_nonblocking(fd)
        _set_winsize(fd, self._rows, self._cols)

        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(fd, self._on_fd_ready)

    # -- I/O ---------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Write input bytes to the child. No-ops after the PTY master is
        closed. Queues + backs off on EAGAIN (full PTY buffer)."""
        if self._fd is None or not data:
            return
        # If we already have pending bytes, append — the writer-drainer
        # will flush everything in order.
        if self._write_queue:
            self._write_queue.extend(data)
            self._ensure_writer()
            return
        try:
            written = os.write(self._fd, data)
        except OSError as e:
            if e.errno in (errno.EBADF, errno.EIO):
                self._handle_exit()
                return
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                self._write_queue.extend(data)
                self._ensure_writer()
                return
            self._record_error(f"write errno {e.errno}: {e}")
            raise
        if written < len(data):
            self._write_queue.extend(data[written:])
            self._ensure_writer()

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        self._rows = rows
        self._cols = cols
        if self._fd is None:
            return
        try:
            _set_winsize(self._fd, rows, cols)
        except OSError as e:
            # ENOTTY / EBADF mean the fd is no longer a pty (child exited
            # mid-resize). Record and proceed with exit handling.
            self._record_error(f"resize ioctl errno {e.errno}: {e}")
            if e.errno in (errno.EBADF, errno.ENOTTY):
                self._handle_exit()

    def close(self, signal_no: int = signal.SIGTERM) -> None:
        """Signal + reap the child. Escalates to SIGKILL if the child ignores
        signal_no. Idempotent. Fires on_exit exactly once.

        Uses os.kill(pid, 0) to probe liveness rather than WNOHANG waitpid
        so we don't reap here — reaping happens in a single place (_reap)
        so exit-status translation lives in one place.
        """
        if self._pid is None and self._exit_fired:
            return
        if self._pid is not None:
            try:
                os.kill(self._pid, signal_no)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + _TERM_DEADLINE_S
            while time.monotonic() < deadline:
                try:
                    os.kill(self._pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(_TERM_POLL_S)
            else:
                try:
                    os.kill(self._pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._handle_exit()

    def kill(self) -> None:
        self.close(signal.SIGKILL)

    def shutdown_kill(self) -> None:
        """Non-blocking shutdown variant for use inside Textual's on_unmount.

        Sends SIGKILL immediately, does a single WNOHANG waitpid, and
        returns — never spin-waits and never calls blocking waitpid. Leaves
        any straggler as a zombie (the OS will reap it when actor-sh exits).

        `close()` blocks the asyncio loop long enough to guarantee a clean
        reap; that's wrong during app teardown because the event loop is
        itself being torn down and Ctrl+C lands inside our wait.
        """
        if self._exit_fired:
            return
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._exit_fired = True
        if self._fd is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._fd)
            except (ValueError, KeyError) as e:
                self._record_error(f"shutdown remove_reader: {e!r}")
            self._unregister_writer()
        if self._pid is not None:
            already_reaped = False
            try:
                pid, status = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                # ECHILD: child already reaped elsewhere (asyncio child
                # watcher, parent's SIGCHLD handler, etc). Record it so
                # a future bug involving double-reap or pid reuse is
                # visible in diagnostics.
                self._record_error(
                    f"shutdown_kill: pid {self._pid} already reaped (ECHILD)",
                )
                self._exit_code = -int(signal.SIGKILL)
                already_reaped = True

            if already_reaped:
                pass
            elif pid == 0:
                # Still alive; don't block. Accept a transient zombie.
                self._exit_code = -int(signal.SIGKILL)
                self._record_error(
                    f"shutdown_kill: pid {self._pid} not reaped — left as zombie",
                )
            elif os.WIFEXITED(status):
                self._exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                self._exit_code = -os.WTERMSIG(status)
            else:
                self._exit_code = -1
            self._pid = None
        self._close_fd()
        if self._on_exit is not None and self._exit_code is not None:
            try:
                self._on_exit(self._exit_code)
            except Exception as e:
                self._record_error(f"shutdown on_exit raised: {e!r}")

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
            self._record_error(f"read errno {e.errno}: {e}")
            raise
        if not data:
            self._handle_exit()
            return
        # Auto-reply to terminal-capability queries before forwarding
        # the chunk to the screen. Without these replies, codex (and
        # other modern TUIs that gate startup on a DA1 / OSC 11 round-
        # trip) stall indefinitely or render in degraded mode. Replies
        # are written to the pty master via the same write() path
        # keypresses use.
        replies = _build_query_replies(data)
        if replies:
            self.write(replies)
        if self._on_output is not None:
            try:
                self._on_output(data)
            except Exception as e:  # pragma: no cover — defensive
                self._record_error(f"on_output raised: {e!r}")

    def _ensure_writer(self) -> None:
        if self._writer_registered or self._fd is None or self._loop is None:
            return
        self._loop.add_writer(self._fd, self._drain_writer)
        self._writer_registered = True

    def _drain_writer(self) -> None:
        if self._fd is None or not self._write_queue:
            self._unregister_writer()
            return
        try:
            written = os.write(self._fd, bytes(self._write_queue))
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            if e.errno in (errno.EBADF, errno.EIO):
                self._unregister_writer()
                self._handle_exit()
                return
            self._record_error(f"drain errno {e.errno}: {e}")
            self._unregister_writer()
            return
        del self._write_queue[:written]
        if not self._write_queue:
            self._unregister_writer()

    def _unregister_writer(self) -> None:
        if not self._writer_registered or self._fd is None or self._loop is None:
            return
        try:
            self._loop.remove_writer(self._fd)
        except (ValueError, KeyError) as e:
            self._record_error(f"remove_writer: {e!r}")
        self._writer_registered = False

    def _handle_exit(self) -> None:
        if self._exit_fired:
            return
        self._exit_fired = True
        if self._fd is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._fd)
            except (ValueError, KeyError) as e:
                self._record_error(f"remove_reader: {e!r}")
            self._unregister_writer()
        self._reap()
        if self._on_exit is not None and self._exit_code is not None:
            self._on_exit(self._exit_code)

    def _reap(self) -> None:
        if self._pid is None:
            if self._exit_code is None:
                self._exit_code = -1
            self._close_fd()
            return
        status: Optional[int] = None
        # Try WNOHANG first; then poll up to _REAP_DEADLINE_S so we don't
        # block the asyncio loop indefinitely on paths where close() didn't
        # SIGKILL (e.g. EOF on the master fd while the child is still
        # running briefly). Escalate to SIGKILL if the deadline expires.
        deadline = time.monotonic() + _REAP_DEADLINE_S
        while True:
            try:
                pid, status = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                if self._exit_code is None:
                    self._exit_code = -1
                self._pid = None
                self._close_fd()
                return
            if pid != 0:
                break
            if time.monotonic() >= deadline:
                try:
                    os.kill(self._pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # One last blocking wait now that SIGKILL is in flight —
                # guaranteed to return in ~ms.
                try:
                    pid, status = os.waitpid(self._pid, 0)
                except ChildProcessError:
                    if self._exit_code is None:
                        self._exit_code = -1
                    self._pid = None
                    self._close_fd()
                    return
                break
            time.sleep(_REAP_POLL_S)
        if status is None:
            self._exit_code = -1
        elif os.WIFEXITED(status):
            self._exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self._exit_code = -os.WTERMSIG(status)
        else:
            self._exit_code = -1
        self._pid = None
        self._close_fd()

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def _record_error(self, note: str) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.ERROR, note=note)


# --- low-level helpers ----------------------------------------------------

def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
