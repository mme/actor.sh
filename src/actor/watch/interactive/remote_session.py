"""Watch-side adapter that drives an `InteractiveSession` (gRPC bidi
stream against actord) through the same surface the embedded
TerminalWidget already speaks to a local `PtySession` with.

Phase 2.5: the daemon owns the PTY. This adapter:
- spawns a reader task that pulls ServerFrames off the stream and
  routes stdout/stderr bytes to the widget's on_output callback;
- exposes write / resize / close that translate to ClientFrames;
- fires on_exit once when the daemon's ExitInfo arrives or the
  stream errors out.

Mirrors the subset of `PtySession` that the widget + manager actually
touch (write, resize, close, set_callbacks, pid, exit_code, exited,
spawn). `pid`/`fd` always read None — they're daemon-side concepts
and the watch widget doesn't actually need them; manager uses
`pid` only as a flag-style check before update_run_pid (we skip
that call entirely on the remote path because the daemon owns the
run-row writes).
"""
from __future__ import annotations

import asyncio
import signal
from typing import Callable, Optional

from ...service import InteractiveSession
from .diagnostics import DiagnosticRecorder, EventKind


class RemotePtySession:

    def __init__(
        self,
        session: InteractiveSession,
        *,
        on_output: Optional[Callable[[bytes], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
        recorder: Optional[DiagnosticRecorder] = None,
    ) -> None:
        self._session = session
        self._on_output = on_output
        self._on_exit = on_exit
        self._recorder = recorder
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._exit_code: Optional[int] = None
        self._exit_fired = False

    # -- API mirroring PtySession ----------------------------------------

    def set_callbacks(
        self,
        *,
        on_output: Optional[Callable[[bytes], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
    ) -> None:
        if on_output is not None:
            self._on_output = on_output
        if on_exit is not None:
            self._on_exit = on_exit

    def spawn(self) -> None:
        """Start the read loop. The daemon already spawned the agent
        when the stream opened — `spawn` here just wires up the local
        async task that decodes ServerFrames."""
        if self._reader_task is not None:
            raise RuntimeError("already spawned")
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._read_loop())

    def write(self, data: bytes) -> None:
        if not data or self._exit_fired:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_send_stdin(data))

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0 or self._exit_fired:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_send_resize(cols, rows))

    def close(self, signal_no: int = signal.SIGTERM) -> None:
        """Ask the daemon to terminate the child by forwarding the
        signal, then close the local stream. Idempotent."""
        if self._exit_fired:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._async_close(signal_no))

    def kill(self) -> None:
        self.close(signal.SIGKILL)

    def shutdown_kill(self) -> None:
        """Watch app teardown variant. Fire-and-forget close; the
        daemon handles cleanup once the stream goes away."""
        if self._exit_fired:
            return
        self._exit_fired = True
        if self._exit_code is None:
            self._exit_code = -int(signal.SIGKILL)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._async_close(signal.SIGKILL))
        if self._on_exit is not None:
            try:
                self._on_exit(self._exit_code)
            except Exception as e:
                self._record_error(f"shutdown on_exit raised: {e!r}")

    # -- properties matching PtySession ----------------------------------

    @property
    def pid(self) -> Optional[int]:
        # PID lives on the daemon side; watch doesn't track it.
        return None

    @property
    def fd(self) -> Optional[int]:
        # No local pty fd on the remote path.
        return None

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def exited(self) -> bool:
        return self._exit_code is not None

    # -- internals -------------------------------------------------------

    async def _safe_send_stdin(self, data: bytes) -> None:
        try:
            await self._session.send_stdin(data)
        except Exception as e:
            self._record_error(f"send_stdin: {e!r}")

    async def _safe_send_resize(self, cols: int, rows: int) -> None:
        try:
            await self._session.send_resize(cols, rows)
        except Exception as e:
            self._record_error(f"send_resize: {e!r}")

    async def _async_close(self, signal_no: int) -> None:
        try:
            await self._session.send_signal(signal_no)
        except Exception as e:
            self._record_error(f"send_signal: {e!r}")
        # `__aexit__` on the session is what closes the channel; that
        # happens in the manager's lifecycle hook.

    async def _read_loop(self) -> None:
        try:
            while True:
                frame = await self._session.recv()
                if frame is None or frame.kind == "exit":
                    break
                if frame.kind in ("stdout", "stderr"):
                    data = frame.value or b""
                    if self._on_output is not None and data:
                        try:
                            self._on_output(data)
                        except Exception as e:
                            self._record_error(f"on_output: {e!r}")
        except (asyncio.CancelledError, Exception) as e:
            if not isinstance(e, asyncio.CancelledError):
                self._record_error(f"read_loop: {e!r}")
        finally:
            self._fire_exit_once()

    def _fire_exit_once(self) -> None:
        if self._exit_fired:
            return
        self._exit_fired = True
        if self._exit_code is None:
            # Pull from the InteractiveSession if it captured ExitInfo.
            try:
                self._exit_code = (
                    self._session._exit_code  # type: ignore[attr-defined]
                    if self._session._exit_code is not None  # type: ignore[attr-defined]
                    else -1
                )
            except Exception:
                self._exit_code = -1
        if self._on_exit is not None:
            try:
                self._on_exit(self._exit_code)
            except Exception as e:
                self._record_error(f"on_exit: {e!r}")

    def _record_error(self, note: str) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.ERROR, note=note)
