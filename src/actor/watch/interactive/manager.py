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
from ...types import Run, Status, _now_iso
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
        config: dict,
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

        run_id = self._insert_run(actor_name, config)

        info = InteractiveSession(
            actor_name=actor_name,
            session=pty_session,
            screen=screen,
            widget=widget,
            run_id=run_id,
        )
        self._sessions[actor_name] = info

        # Chain on_exit: preserve the widget's handler (which posts the
        # SessionExited message for UI recovery) AND add DB finalization.
        widget_on_exit = pty_session._on_exit  # set by widget constructor

        def _chained_on_exit(code: int) -> None:
            try:
                self._finalize_run(actor_name, info.run_id, code)
            finally:
                if widget_on_exit is not None:
                    widget_on_exit(code)
        pty_session.set_callbacks(on_exit=_chained_on_exit)

        pty_session.spawn()
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

    # -- DB integration ----------------------------------------------------

    def _insert_run(self, actor_name: str, config: dict) -> int:
        with self._db_opener() as db:
            run = Run(
                id=0,
                actor_name=actor_name,
                prompt=INTERACTIVE_PROMPT,
                status=Status.RUNNING,
                exit_code=None,
                pid=None,
                config=dict(config),
                started_at=_now_iso(),
                finished_at=None,
            )
            run_id = db.insert_run(run)
            db.touch_actor(actor_name)
            return run_id

    def _finalize_run(self, actor_name: str, run_id: int, exit_code: int) -> None:
        """Idempotent, never raises. Errors are routed to the diagnostic
        recorder so the nested-finally in `close()` can rely on it."""
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
                db.update_run_status(run_id, final, exit_code)
        except Exception as e:
            # DB errors at shutdown shouldn't crash the TUI.
            self._record_error(f"finalize_run({actor_name!r}, {run_id}): {e!r}")

    def _record_error(self, note: str) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.ERROR, note=note)
