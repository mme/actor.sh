"""Per-actor PtySession + TerminalWidget registry.

The watch app owns one InteractiveSessionManager. It tracks live sessions
keyed by actor name, hands out widgets for the selected actor, and kills
everything on app shutdown.

Also integrates with the Database: on create, inserts an *interactive*
Run row; on session exit, updates it to DONE/ERROR/STOPPED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ...commands import INTERACTIVE_PROMPT
from ...db import Database
from ...interfaces import Agent
from ...types import ActorConfig, Run, Status, _now_iso
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
    # Closure that returns the current session-file byte size, used to
    # stamp `log_end_offset` at finalize time. Stored on the dataclass
    # so `close()` / `shutdown()` can reach the right agent + path
    # without having to re-plumb them through every caller — the
    # closure captures what `create()` already had in scope.
    stat_size: Callable[[], Optional[int]] = field(
        default=lambda: None,
    )


class InteractiveSessionManager:
    """Owns live interactive sessions for the watch app."""

    DEFAULT_ROWS = 24
    DEFAULT_COLS = 80

    def __init__(
        self,
        db_opener: Callable[[], Database],
        *,
        recorder: Optional[DiagnosticRecorder] = None,
    ) -> None:
        self._db_opener = db_opener
        self._sessions: Dict[str, InteractiveSession] = {}
        self._recorder = recorder
        # Sessions closed explicitly via close()/close_all() should be
        # marked STOPPED rather than DONE/ERROR on finalize.
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

        Raises if one is already live for this actor (caller should close
        first, or call `get()` to reuse).
        """
        if actor_name in self._sessions:
            raise RuntimeError(f"interactive session already exists for {actor_name!r}")

        argv = agent.interactive_argv(session_id, config)
        pty_session = PtySession(
            argv=argv,
            cwd=cwd,
            rows=self.DEFAULT_ROWS,
            cols=self.DEFAULT_COLS,
            recorder=self._recorder,
        )
        screen = TerminalScreen(rows=self.DEFAULT_ROWS, cols=self.DEFAULT_COLS)
        widget = TerminalWidget(pty_session, screen, recorder=self._recorder)

        # Snap start-offset BEFORE spawning so entries the PTY's child
        # writes are bracketed above this value.
        start_offset = agent.session_file_size(cwd, session_id)
        if start_offset is None:
            start_offset = 0

        run_id = self._insert_run(actor_name, config, start_offset)

        info = InteractiveSession(
            actor_name=actor_name,
            session=pty_session,
            screen=screen,
            widget=widget,
            run_id=run_id,
            stat_size=lambda: agent.session_file_size(cwd, session_id),
        )
        self._sessions[actor_name] = info

        # Chain on_exit: preserve the widget's handler (which posts the
        # SessionExited message for UI recovery) AND add DB finalization.
        # The finalize path stats the session file one more time to
        # stamp `log_end_offset`, closing the run's byte bucket.
        widget_on_exit = pty_session._on_exit  # set by widget constructor

        def _chained_on_exit(code: int) -> None:
            try:
                end_offset = agent.session_file_size(cwd, session_id)
                self._finalize_run(actor_name, info.run_id, code, end_offset)
            finally:
                if widget_on_exit is not None:
                    widget_on_exit(code)
        pty_session.set_callbacks(on_exit=_chained_on_exit)

        pty_session.spawn()
        # Record the pid so resolve_actor_status can see the child is alive;
        # without this the Run is classified ERROR by the liveness probe.
        # The session is already live at this point — a DB hiccup here
        # shouldn't kill the spawn, so we record and move on.
        if pty_session.pid is not None:
            try:
                with self._db_opener() as db:
                    db.update_run_pid(run_id, pty_session.pid)
            except Exception as e:
                self._record_error(
                    f"create({actor_name!r}) update_run_pid failed: {e!r}",
                )
        return info

    def close(self, actor_name: str) -> None:
        """App-initiated teardown. Run is marked STOPPED (not ERROR) so
        the status distinguishes `quit watch` from the child exiting."""
        info = self._sessions.pop(actor_name, None)
        if info is None:
            return
        self._stopping.add(actor_name)
        try:
            info.session.close()
        finally:
            # Always finalize the run, even if session.close() raises
            # (incl. KeyboardInterrupt during the close poll). Otherwise
            # the DB row would stay RUNNING forever.
            try:
                end_offset = info.stat_size()
                self._finalize_run(
                    actor_name, info.run_id,
                    info.session.exit_code or -1, end_offset,
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
        """Non-blocking variant for Textual app teardown. SIGKILLs every
        live session, finalizes each Run as STOPPED, returns fast. Any
        child that doesn't reap inside the WNOHANG window is left as a
        transient zombie and the OS cleans up when actor-sh exits."""
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
                    end_offset = info.stat_size()
                    self._finalize_run(
                        name, info.run_id,
                        info.session.exit_code or -1, end_offset,
                    )
                except Exception as e:
                    self._record_error(f"shutdown finalize({name!r}): {e!r}")
            finally:
                self._stopping.discard(name)

    # -- DB integration ----------------------------------------------------

    def _insert_run(
        self, actor_name: str, config: ActorConfig, start_offset: int,
    ) -> int:
        with self._db_opener() as db:
            run = Run(
                id=0,
                actor_name=actor_name,
                prompt=INTERACTIVE_PROMPT,
                status=Status.RUNNING,
                exit_code=None,
                pid=None,
                config=config,
                started_at=_now_iso(),
                finished_at=None,
                log_start_offset=start_offset,
            )
            run_id = db.insert_run(run)
            db.touch_actor(actor_name)
            return run_id

    def _finalize_run(
        self,
        actor_name: str,
        run_id: int,
        exit_code: int,
        log_end_offset: Optional[int] = None,
    ) -> None:
        """Idempotent, never raises. Errors are routed to the diagnostic
        recorder so the nested-finally in `close()` can rely on it.

        `log_end_offset` (when provided) closes the run's byte bucket
        for correlation. Passed as None from call sites that don't
        have a current file-size reading; the row stays with
        log_end_offset=NULL and the bucketing helper falls back to
        the next run's start_offset or the current file size."""
        try:
            with self._db_opener() as db:
                current = db.get_run(run_id)
                if current is None:
                    return
                if current.status in (Status.DONE, Status.ERROR, Status.STOPPED):
                    return
                if actor_name in self._stopping:
                    final = Status.STOPPED
                else:
                    final = Status.DONE if exit_code == 0 else Status.ERROR
                db.update_run_status(
                    run_id, final, exit_code, log_end_offset=log_end_offset,
                )
        except Exception as e:
            # DB errors at shutdown shouldn't crash the TUI.
            self._record_error(f"finalize_run({actor_name!r}, {run_id}): {e!r}")

    def _record_error(self, note: str) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.ERROR, note=note)
