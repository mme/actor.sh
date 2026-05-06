"""Per-actor RemotePtySession + TerminalWidget registry.

The watch app owns one InteractiveSessionManager. It tracks live
sessions keyed by actor name, hands out widgets for the selected
actor, and kills everything on app shutdown.

Phase 2.5: every interactive session runs through actord. The manager
opens an `InteractiveSession` gRPC bidi stream against the
`RemoteActorService`, wraps it in a `RemotePtySession` adapter that
mirrors the local-PTY interface the widget expects, and keeps the
gRPC channel alive for the session's lifetime. The daemon owns the
PTY and writes the run-row state — the manager never inserts /
updates / finalizes Run rows itself; closing the gRPC stream is what
triggers the daemon's finalize.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ...interfaces import Agent
from ...service import (
    ActorService,
    InteractiveSession as RemoteInteractive,
    RemoteActorService,
)
from ...types import ActorConfig
from .diagnostics import DiagnosticRecorder, EventKind
from .remote_session import RemotePtySession
from .screen import TerminalScreen
from .widget import TerminalWidget


@dataclass(frozen=True)
class InteractiveSession:
    actor_name: str
    session: RemotePtySession
    screen: TerminalScreen
    widget: TerminalWidget
    remote: RemoteInteractive


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

    async def create(
        self,
        actor_name: str,
        agent: Agent,
        session_id: str,
        cwd: Path,
        config: ActorConfig,
    ) -> InteractiveSession:
        """Open a new interactive session through actord.

        Raises if one is already live for this actor (caller should
        close first, or call `get()` to reuse).

        `agent`, `session_id`, `cwd`, and `config` are accepted for
        compatibility with the watch-app caller but no longer drive
        the spawn — the daemon picks the agent argv based on the
        actor's stored agent_session and does the PTY work itself.
        """
        if actor_name in self._sessions:
            raise RuntimeError(
                f"interactive session already exists for {actor_name!r}"
            )

        svc = self._service_factory()
        if not isinstance(svc, RemoteActorService):
            raise RuntimeError(
                "InteractiveSessionManager requires a RemoteActorService "
                "in Phase 2.5; the daemon owns the PTY"
            )

        remote = svc.interactive_session(
            actor_name, cols=self.DEFAULT_COLS, rows=self.DEFAULT_ROWS,
        )
        # Open the bidi stream eagerly so we surface "actor has no
        # session" / "actor not found" errors here rather than on
        # the first widget keystroke.
        await remote.__aenter__()

        adapter = RemotePtySession(remote, recorder=self._recorder)
        screen = TerminalScreen(rows=self.DEFAULT_ROWS, cols=self.DEFAULT_COLS)
        widget = TerminalWidget(adapter, screen, recorder=self._recorder)

        info = InteractiveSession(
            actor_name=actor_name,
            session=adapter,
            screen=screen,
            widget=widget,
            remote=remote,
        )
        self._sessions[actor_name] = info

        # The widget's __init__ wires its own on_output / on_exit
        # callbacks via set_callbacks. Re-read the widget-installed
        # on_exit so we can chain a "drop the session out of the
        # registry" finishing step on top.
        widget_on_exit = adapter._on_exit  # type: ignore[attr-defined]

        def _chained_on_exit(code: int) -> None:
            try:
                self._sessions.pop(actor_name, None)
            finally:
                if widget_on_exit is not None:
                    widget_on_exit(code)
        adapter.set_callbacks(on_exit=_chained_on_exit)

        adapter.spawn()
        return info

    async def close(self, actor_name: str) -> None:
        """App-initiated teardown. Closes the gRPC stream; the daemon
        reaps the child and finalizes the run row."""
        info = self._sessions.pop(actor_name, None)
        if info is None:
            return
        self._stopping.add(actor_name)
        try:
            info.session.close()
        finally:
            try:
                await info.remote.__aexit__(None, None, None)
            except Exception as e:
                self._record_error(f"close({actor_name!r}) stream: {e!r}")
            self._stopping.discard(actor_name)

    async def close_all(self) -> None:
        for name in list(self._sessions.keys()):
            try:
                await self.close(name)
            except Exception as e:
                self._record_error(f"close_all({name!r}): {e!r}")

    async def shutdown(self) -> None:
        """Non-blocking teardown for Textual's on_unmount. Closes
        every gRPC stream — the daemon reaps the children and
        finalizes their run rows. Any straggler is left for the
        daemon's own shutdown to handle."""
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
                    await info.remote.__aexit__(None, None, None)
                except Exception as e:
                    self._record_error(
                        f"shutdown({name!r}) stream: {e!r}",
                    )
            finally:
                self._stopping.discard(name)

    # -- diagnostics ------------------------------------------------------

    def _record_error(self, note: str) -> None:
        if self._recorder is not None:
            self._recorder.record(EventKind.ERROR, note=note)
