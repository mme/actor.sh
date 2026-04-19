"""Per-actor PtySession + TerminalWidget registry.

The watch app owns one InteractiveSessionManager. It tracks live sessions
keyed by actor name, hands out widgets for the selected actor, and kills
everything on app shutdown.

Also integrates with the Database: on create, inserts an *interactive*
Run row; on session exit, updates it to DONE/ERROR/STOPPED.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ...commands import INTERACTIVE_PROMPT
from ...db import Database
from ...interfaces import Agent
from ...types import Run, Status, _now_iso
from .diagnostics import DiagnosticRecorder
from .pty_session import PtySession
from .screen import TerminalScreen
from .widget import TerminalWidget


@dataclass
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

        # Side-channel: when the child exits naturally, close + update DB.
        def _on_exit(code: int) -> None:
            self._finalize_run(actor_name, info.run_id, code)
        widget._session._on_exit = _on_exit  # type: ignore[attr-defined]

        pty_session.spawn()
        return info

    def close(self, actor_name: str) -> None:
        """Kill the session (if running) and remove it from the registry."""
        info = self._sessions.pop(actor_name, None)
        if info is None:
            return
        info.session.close()
        # _on_exit will have fired via close() -> _handle_exit().
        # If for some reason it didn't (race), make sure the DB row doesn't
        # stay RUNNING forever.
        self._finalize_run(actor_name, info.run_id, info.session.exit_code or -1)

    def close_all(self) -> None:
        for name in list(self._sessions.keys()):
            self.close(name)

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
        with self._db_opener() as db:
            current = db.get_run(run_id)
            if current is None:
                return
            # Don't overwrite a STOPPED status set externally.
            if current.status == Status.STOPPED:
                return
            # Don't double-finalize if already done.
            if current.status in (Status.DONE, Status.ERROR):
                return
            final = Status.DONE if exit_code == 0 else Status.ERROR
            db.update_run_status(run_id, final, exit_code)
