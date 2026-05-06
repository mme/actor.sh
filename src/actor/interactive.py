"""CLI-side driver for `RemoteActorService.interactive_session`.

The CLI puts the local terminal in raw mode, opens a bidi gRPC stream
against actord, and proxies bytes between the user's TTY and the
daemon-spawned PTY. The daemon owns the PTY master; this side just
moves bytes and a few control frames (resize on SIGWINCH, signal on
Ctrl-C).

Out of scope for now: line-discipline / cooked mode, mouse passthrough,
non-TTY stdin (the user's stdin must be a real terminal). Those are
trade-offs for a later phase.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import struct
import sys
import termios
import tty
from typing import Optional, Tuple

from .errors import DaemonUnreachableError
from .service import ActorService, RemoteActorService


def _term_size(fd: int) -> Tuple[int, int]:
    """Return (cols, rows) for the given tty fd, or (80, 24) when the
    ioctl fails (e.g. fd isn't a tty)."""
    try:
        import fcntl
        rows, cols, _, _ = struct.unpack(
            "HHHH",
            fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8),
        )
        return cols, rows
    except OSError:
        return 80, 24


@contextlib.contextmanager
def _raw_mode(fd: int):
    """Put a tty fd into cbreak mode for the duration of the block.
    Cbreak (`tty.setcbreak`) keeps signal handling alive — Ctrl-C
    still raises SIGINT in the parent — while disabling line buffering
    so each keystroke arrives on stdin immediately.

    No-op when fd isn't a tty (e.g. in tests under PIPE)."""
    try:
        original = termios.tcgetattr(fd)
    except termios.error:
        yield
        return
    try:
        tty.setcbreak(fd)
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
        except termios.error:
            pass


async def run_interactive_cli(
    service: ActorService,
    *,
    name: str,
    stdin_fd: int = 0,
    stdout_fd: int = 1,
) -> Tuple[int, str]:
    """Drive an `InteractiveSession` against the daemon. The local
    terminal goes into cbreak mode; stdin bytes stream to the
    daemon-side PTY, stdout/stderr bytes from the daemon write back
    to the local TTY. SIGWINCH triggers a resize frame; SIGINT
    forwards as a signal frame to the child.

    Returns `(exit_code, friendly_message)` to mirror the legacy
    `LocalActorService.interactive_actor` shape — same return type,
    same exit-code semantics."""
    if not isinstance(service, RemoteActorService):
        raise RuntimeError(
            "actor run -i requires a RemoteActorService; "
            "no local fallback in Phase 2.5"
        )

    cols, rows = _term_size(stdout_fd)
    loop = asyncio.get_running_loop()

    try:
        async with service.interactive_session(name, cols=cols, rows=rows) as session:
            stdin_task: Optional[asyncio.Task[None]] = None
            recv_task: Optional[asyncio.Task[None]] = None
            sigwinch_handler = None
            sigint_handler = None

            stop_input = asyncio.Event()

            async def _pump_recv() -> None:
                while True:
                    frame = await session.recv()
                    if frame is None:
                        return
                    if frame.kind == "stdout":
                        os.write(stdout_fd, frame.stdout or b"")
                    elif frame.kind == "stderr":
                        os.write(stdout_fd, frame.stderr or b"")
                    elif frame.kind == "exit":
                        return  # ExitInfo arrived; recv() captured it

            async def _pump_stdin_from(reader: asyncio.StreamReader) -> None:
                while not stop_input.is_set():
                    try:
                        chunk = await reader.read(4096)
                    except (asyncio.CancelledError, OSError):
                        return
                    if not chunk:
                        await session.end_input()
                        return
                    try:
                        await session.send_stdin(chunk)
                    except Exception:
                        return

            with _raw_mode(stdin_fd):
                # Wire stdin via an asyncio reader so we can cancel it
                # cleanly when the session ends.
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                stdin_pipe = os.fdopen(stdin_fd, "rb", closefd=False)
                try:
                    transport, _ = await loop.connect_read_pipe(
                        lambda: protocol, stdin_pipe,
                    )
                except (ValueError, OSError):
                    transport = None  # stdin isn't a real fd; stay on the
                                      # recv-only path

                if transport is not None:
                    stdin_task = asyncio.create_task(_pump_stdin_from(reader))

                # Forward SIGWINCH as a resize frame.
                def _on_sigwinch() -> None:
                    new_cols, new_rows = _term_size(stdout_fd)
                    asyncio.run_coroutine_threadsafe(
                        session.send_resize(new_cols, new_rows), loop,
                    )
                try:
                    loop.add_signal_handler(signal.SIGWINCH, _on_sigwinch)
                    sigwinch_handler = signal.SIGWINCH
                except (NotImplementedError, ValueError):
                    sigwinch_handler = None

                # Forward SIGINT to the child instead of letting it
                # tear down the local CLI.
                def _on_sigint() -> None:
                    asyncio.run_coroutine_threadsafe(
                        session.send_signal(signal.SIGINT), loop,
                    )
                try:
                    loop.add_signal_handler(signal.SIGINT, _on_sigint)
                    sigint_handler = signal.SIGINT
                except (NotImplementedError, ValueError):
                    sigint_handler = None

                recv_task = asyncio.create_task(_pump_recv())
                try:
                    await recv_task
                finally:
                    stop_input.set()
                    if sigwinch_handler is not None:
                        try:
                            loop.remove_signal_handler(sigwinch_handler)
                        except (NotImplementedError, ValueError):
                            pass
                    if sigint_handler is not None:
                        try:
                            loop.remove_signal_handler(sigint_handler)
                        except (NotImplementedError, ValueError):
                            pass
                    if stdin_task is not None and not stdin_task.done():
                        stdin_task.cancel()
                        try:
                            await stdin_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    if transport is not None:
                        try:
                            transport.close()
                        except Exception:
                            pass

            exit_code = await session.exit_code()

        # Mirror the legacy `interactive_actor` message format so
        # tests / users see the same final line.
        if session.final_status is not None:
            from .types import Status
            if session.final_status == Status.STOPPED:
                return exit_code, f"Interactive session for '{name}' stopped."
        return exit_code, (
            f"Interactive session for '{name}' ended (exit {exit_code})."
        )
    except DaemonUnreachableError:
        raise
