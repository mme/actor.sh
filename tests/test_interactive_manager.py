"""Tests for InteractiveSessionManager.

Manager coordinates three moving parts: PtySession lifetime, TerminalWidget
chain-of-callbacks, and DB Run finalization. These are the cases
multi-reviewer calls flagged as a coverage gap.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

if sys.platform == "win32":
    raise unittest.SkipTest("PTY not available on Windows")

from actor import INTERACTIVE_PROMPT, LocalActorService
from actor.db import Database
from actor.git import RealGit
from actor.process import RealProcessManager
from actor.types import ActorConfig, Status
from actor.watch.interactive.manager import InteractiveSessionManager
from actor.watch.interactive.diagnostics import DiagnosticRecorder, EventKind


def _find_binary(name: str) -> str:
    for base in ("/bin", "/usr/bin"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    path = shutil.which(name)
    if path is None:
        raise unittest.SkipTest(f"{name} not found on PATH")
    return path


class _FakeAgent:
    """Minimal Agent implementation for manager tests. Only interactive_argv
    is exercised; the rest raises to catch unintended uses."""

    def __init__(self, argv: list[str]) -> None:
        self._argv = argv

    def interactive_argv(self, session_id: str, config: ActorConfig) -> list[str]:
        return list(self._argv)

    def start(self, *a, **k):  # pragma: no cover
        raise AssertionError("start should not be called in manager tests")

    def resume(self, *a, **k):  # pragma: no cover
        raise AssertionError("resume should not be called")

    def wait(self, *a, **k):  # pragma: no cover
        raise AssertionError("wait should not be called")

    def read_logs(self, *a, **k):  # pragma: no cover
        return []

    def stop(self, *a, **k):  # pragma: no cover
        raise AssertionError("stop should not be called")


async def _ensure_actor_async(
    db: Database, name: str, session: str = "sess-1",
) -> None:
    """Insert an actor via LocalActorService.new_actor so we don't
    hardcode schema."""
    from tests.test_actor import FakeGit
    svc = LocalActorService(
        db=db,
        git=FakeGit(),
        proc_mgr=None,
        agent_factory=lambda _k: None,
    )
    await svc.new_actor(
        name=name, dir="/tmp", no_worktree=True, base=None,
        agent_name="claude", config=ActorConfig(),
    )
    db.update_actor_session(name, session)


def _ensure_actor(db: Database, name: str, session: str = "sess-1") -> None:
    """Sync bridge for setUp paths that have no running loop yet."""
    asyncio.run(_ensure_actor_async(db, name, session))


def _service_factory(db_path: str):
    """Build a fresh `LocalActorService` per call — the manager opens a
    new DB connection on every mutation. Real watch app uses
    `RealGit` / `RealProcessManager`; manager tests don't exercise
    those paths so any value works."""
    def _factory():
        return LocalActorService(
            db=Database.open(db_path),
            git=RealGit(),
            proc_mgr=RealProcessManager(),
            agent_factory=lambda _k: None,
        )
    return _factory


class InteractiveSessionManagerTests(unittest.IsolatedAsyncioTestCase):
    """Uses /bin/cat as the real subprocess so PTY spawn/exit paths are exercised."""

    def setUp(self) -> None:
        # Use a file-backed sqlite so each db_opener() call can open a
        # fresh connection (the manager uses `with opener()` which closes
        # the connection on exit).
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "actor.db")
        # Seed the schema + actors via one connection, then discard it.
        with Database.open(self._db_path) as db:
            _ensure_actor(db, "alice")
            _ensure_actor(db, "bob")
        self.db = Database.open(self._db_path)  # reader for assertions
        self.recorder = DiagnosticRecorder(capacity=100)
        self.manager = InteractiveSessionManager(
            service_factory=_service_factory(self._db_path),
            recorder=self.recorder,
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    async def test_create_spawns_and_inserts_run(self):
        cat = _find_binary("cat")
        info = await self.manager.create(
            actor_name="alice",
            agent=_FakeAgent([cat]),
            session_id="sess-1",
            cwd=Path("/tmp"),
            config=ActorConfig(),
        )
        self.assertTrue(self.manager.has("alice"))
        self.assertIs(self.manager.get("alice"), info)
        self.assertIsNotNone(info.session.pid)
        run = self.db.get_run(info.run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.prompt, INTERACTIVE_PROMPT)
        self.assertEqual(run.status, Status.RUNNING)
        await self.manager.close("alice")

    async def test_duplicate_create_raises(self):
        cat = _find_binary("cat")
        first = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([cat]),
            session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
        )
        try:
            with self.assertRaises(RuntimeError):
                await self.manager.create(
                    actor_name="alice", agent=_FakeAgent([cat]),
                    session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
                )
            # The original session is still registered and alive.
            self.assertIs(self.manager.get("alice"), first)
        finally:
            await self.manager.close("alice")

    async def test_close_marks_run_stopped(self):
        cat = _find_binary("cat")
        info = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([cat]),
            session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
        )
        await self.manager.close("alice")
        self.assertFalse(self.manager.has("alice"))
        run = self.db.get_run(info.run_id)
        self.assertEqual(
            run.status, Status.STOPPED,
            "app-initiated close should mark the run STOPPED, not ERROR",
        )

    async def test_natural_exit_marks_run_error_on_nonzero(self):
        sh = _find_binary("sh")
        info = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([sh, "-c", "exit 3"]),
            session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
        )
        # Let child exit and on_exit chain run.
        for _ in range(200):
            run = self.db.get_run(info.run_id)
            if run and run.status != Status.RUNNING:
                break
            await asyncio.sleep(0.01)
        run = self.db.get_run(info.run_id)
        self.assertEqual(run.status, Status.ERROR)
        self.assertEqual(run.exit_code, 3)
        # Session entry should eventually be removed via the app's
        # on_terminal_widget_session_exited handler. The manager alone
        # doesn't pop on natural exit, so we pop it ourselves.
        if self.manager.has("alice"):
            await self.manager.close("alice")

    async def test_natural_exit_marks_done_on_zero(self):
        sh = _find_binary("sh")
        info = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([sh, "-c", "exit 0"]),
            session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
        )
        for _ in range(200):
            run = self.db.get_run(info.run_id)
            if run and run.status != Status.RUNNING:
                break
            await asyncio.sleep(0.01)
        run = self.db.get_run(info.run_id)
        self.assertEqual(run.status, Status.DONE)
        self.assertEqual(run.exit_code, 0)
        if self.manager.has("alice"):
            await self.manager.close("alice")

    async def test_create_updates_run_pid(self):
        """The Run row must record the PTY's pid so resolve_actor_status
        sees the child as alive (otherwise it flips to ERROR)."""
        cat = _find_binary("cat")
        info = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([cat]),
            session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
        )
        try:
            run = self.db.get_run(info.run_id)
            self.assertEqual(
                run.pid, info.session.pid,
                "Run.pid should match the spawned PTY pid",
            )
            # And resolve_actor_status agrees the actor is RUNNING.
            from actor.process import RealProcessManager
            status = self.db.resolve_actor_status("alice", RealProcessManager())
            self.assertEqual(status, Status.RUNNING)
        finally:
            await self.manager.close("alice")

    async def test_shutdown_marks_runs_stopped_without_blocking(self):
        import time, signal
        sleep_bin = _find_binary("sleep")
        info = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([sleep_bin, "100"]),
            session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
        )
        start = time.monotonic()
        await self.manager.shutdown()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.3, f"shutdown must not block; took {elapsed:.3f}s")
        self.assertEqual(self.manager.live_names(), [])
        run = self.db.get_run(info.run_id)
        self.assertEqual(
            run.status, Status.STOPPED,
            "shutdown should mark every live Run as STOPPED",
        )

    async def test_close_all_kills_every_session(self):
        cat = _find_binary("cat")
        alice = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([cat]),
            session_id="s1", cwd=Path("/tmp"), config=ActorConfig(),
        )
        bob = await self.manager.create(
            actor_name="bob", agent=_FakeAgent([cat]),
            session_id="s2", cwd=Path("/tmp"), config=ActorConfig(),
        )
        await self.manager.close_all()
        self.assertEqual(self.manager.live_names(), [])
        for info in (alice, bob):
            run = self.db.get_run(info.run_id)
            self.assertEqual(run.status, Status.STOPPED)
            self.assertTrue(info.session.exited)

    async def test_finalize_does_not_overwrite_existing_terminal_status(self):
        cat = _find_binary("cat")
        info = await self.manager.create(
            actor_name="alice", agent=_FakeAgent([cat]),
            session_id="s", cwd=Path("/tmp"), config=ActorConfig(),
        )
        # Simulate external stop marking the row DONE before our on_exit.
        self.db.update_run_status(info.run_id, Status.DONE, 0)
        await self.manager.close("alice")
        run = self.db.get_run(info.run_id)
        self.assertEqual(run.status, Status.DONE,
                         "finalize must not overwrite terminal status")


class ManagerDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_all_failure_recorded(self):
        recorder = DiagnosticRecorder()

        class _BadClose:
            """Stand-in session whose close() always raises."""
            exit_code = None
            exited = False

            def close(self):
                raise RuntimeError("boom")

        from actor.watch.interactive.manager import (
            InteractiveSession, InteractiveSessionManager,
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "actor.db")
        with Database.open(db_path) as seed:
            await _ensure_actor_async(seed, "alice")
        mgr = InteractiveSessionManager(
            service_factory=_service_factory(db_path), recorder=recorder,
        )
        # Plant a fake session directly so close_all exercises the except path.
        fake = InteractiveSession(
            actor_name="alice",
            session=_BadClose(),   # type: ignore[arg-type]
            screen=None,           # type: ignore[arg-type]
            widget=None,           # type: ignore[arg-type]
            run_id=999,
        )
        mgr._sessions["alice"] = fake
        await mgr.close_all()
        events = [e for e in recorder.recent() if e.kind == EventKind.ERROR]
        self.assertTrue(
            any("close_all" in e.note for e in events),
            f"expected an ERROR diagnostic for failed close; got {events!r}",
        )


if __name__ == "__main__":
    unittest.main()
