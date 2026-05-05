"""Per-actor PtySession + TerminalWidget registry.

The watch app owns one InteractiveSessionManager. It tracks live
sessions keyed by actor name, hands out widgets for the selected
actor, and kills everything on app shutdown.

State mutation goes through `ActorService` — the manager doesn't open
its own DB connections. PTY handling stays here (forking, raw I/O);
DB ledger updates are delegated to
`service.start_interactive_run` / `update_interactive_run_pid` /
`finalize_interactive_run`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ...interfaces import Agent
from ...service import ActorService
from ...types import ActorConfig
from .diagnostics import DiagnosticRecorder, EventKind
from .pty_session import PtySession
from .screen import TerminalScreen
from .widget import TerminalWidget


@dataclass(frozen=True)
class InteractiveSession:
    actor_name: str
    session: PtySession
    screen: TerminalScreen
    widget: TerminalWidget
    run_id: int


# `service_factory` returns an `ActorService` per call so the manager
# can mirror the prior `db_opener` semantics — a fresh DB connection
# per mutation, scoped to the operation, then closed. Callers that
# already have a long-lived service can pass `lambda: existing` to
# share it.
ServiceFactory = Callable[[], ActorService]


class InteractiveSessionManager:
    """Owns live interactive sessions for the watch app."""

    DEFAULT_ROWS = 24
    DEFAULT_COLS = 80

    def __init__(
        self,
        service_factory: ServiceFactory,
        *,
        recorder: Optional[DiagnosticRecorder] = None,
    ) -> None:
        self._service_factory = service_factory
        self._sessions: Dict[str, InteractiveSession] = {}
        self._recorder = recorder
        # Sessions closed explicitly via close() / close_all() should
        # be marked STOPPED rather than DONE/ERROR on finalize.
        self._stopping: set[str] = set()

    # -- queries ----------------------------------------------------------

    def get(self, actor_name: str) -> Optional[InteractiveSession]:
        return self._sessions.get(actor_name)

    def has(self, actor_name: str) -> bool:
        return actor_name in self._sessions

    def live_names(self) -> List[str]:
        return list(self._sessions.keys())

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        actor_name: str,
        agent: Agent,
        session_id: str,
        cwd: Path,
        config: ActorConfig,
    ) -> InteractiveSession:
        """Spawn a new interactive session for the given actor.

        Raises if one is already live for this actor (caller should
        close first, or call `get()` to reuse).

        Note: `agent` and `session_id` are accepted for backwards
        compatibility with the watch-app caller, but `argv` and the
        run-row insert come from `service.start_interactive_run`.
        """
        if actor_name in self._sessions:
            raise RuntimeError(
                f"interactive session already exists for {actor_name!r}"
            )

        svc = self._service_factory()
        handle = svc.start_interactive_run(actor_name, agent=agent)

        pty_session = PtySession(
            argv=handle.argv,
            cwd=cwd,
            rows=self.DEFAULT_ROWS,
            cols=self.DEFAULT_COLS,
            recorder=self._recorder,
        )
        screen = TerminalScreen(rows=self.DEFAULT_ROWS, cols=self.DEFAULT_COLS)
        widget = TerminalWidget(pty_session, screen, recorder=self._recorder)

        info = InteractiveSession(
            actor_name=actor_name,
            session=pty_session,
            screen=screen,
            widget=widget,
            run_id=handle.run_id,
        )
        self._sessions[actor_name] = info

        # Chain on_exit: preserve the widget's handler (which posts
        # the SessionExited message for UI recovery) AND add DB
        # finalization through the service.
        widget_on_exit = pty_session._on_exit  # set by widget constructor

        def _chained_on_exit(code: int) -> None:
            try:
                self._finalize_run(actor_name, info.run_id, code)
            finally:
                if widget_on_exit is not None:
                    widget_on_exit(code)
        pty_session.set_callbacks(on_exit=_chained_on_exit)

        pty_session.spawn()
        # Record the pid so resolve_actor_status sees the child as
        # alive — without this the Run is classified ERROR by the
        # liveness probe. The session is already live; a DB hiccup
        # here shouldn't kill the spawn.
        if pty_session.pid is not None:
            try:
                self._service_factory().update_interactive_run_pid(
                    handle.run_id, pty_session.pid,
                )
            except Exception as e:
                self._record_error(
                    f"create({actor_name!r}) update_run_pid failed: {e!r}",
                )
        return info

    def close(self, actor_name: str) -> None:
        """App-initiated teardown. Run is marked STOPPED (not ERROR)
        so the status distinguishes `quit watch` from the child
        exiting."""
        info = self._sessions.pop(actor_name, None)
        if info is None:
            return
        self._stopping.add(actor_name)
        try:
            info.session.close()
        finally:
            # Always finalize the run, even if session.close() raises
            # (incl. KeyboardInterrupt during the close poll).
            try:
                self._finalize_run(
                    actor_name, info.run_id, info.session.exit_code or -1,
                )
            finally:
                self._stopping.discard(actor_name)

    def close_all(self) -> None:
        for name in list(self._sessions.keys()):
            try:
                self.close(name)
            except Exception as e:
                self._record_error(f"close_all({name!r}): {e!r}")

    def shutdown(self) -> None:
        """Non-blocking variant for Textual app teardown. SIGKILLs
        every live session, finalizes each Run as STOPPED, returns
        fast. Any child that doesn't reap inside the WNOHANG window
        is left as a transient zombie and the OS cleans up when
        actor-sh exits."""
        for name in list(self._sessions.keys()):
            info = self._sessions.pop(name, None)
            if info is None:
                continue
            self._stopping.add(name)
            try:
                try:
                    info.session.shutdown_kill()
                except Exception as e:
                    self._record_error(f"shutdown({name!r}): {e!r}")
                try:
                    self._finalize_run(
                        name, info.run_id, info.session.exit_code or -1,
                    )
                except Exception as e:
                    self._record_error(f"shutdown finalize({name!r}): {e!r}")
            finally:
                self._stopping.discard(name)

    # -- service integration ----------------------------------------------

    def _finalize_run(
        self, actor_name: str, run_id: int, exit_code: int,
    ) -> None:
        """Idempotent, never raises. Errors are routed to the
        diagnostic recorder so the nested-finally in `close()` can
        rely on it.

        When `actor_name` is in `_stopping` we override the service's
        natural DONE/ERROR derivation to STOPPED — preserves the
        prior "app-initiated close marks STOPPED" semantics."""
        from ...types import Status
        force_status = Status.STOPPED if actor_name in self._stopping else None
        try:
            self._service_factory().finalize_interactive_run(
                run_id, exit_code, force_status=force_status,
            )
        except Exception as e:
            self._record_error(
                f"finalize_run({actor_name!r}, {run_id}): {e!r}"
            )

    def _record_error(self, note: str) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.ERROR, note=note)
