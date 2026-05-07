"""Tests for InteractiveSessionManager.

Phase 2.5: the manager opens a `RemoteActorService.interactive_session`
bidi stream against a real `actord` and wraps it in a
`RemotePtySession` adapter. The daemon owns the PTY and finalizes the
run row; this test file exercises the manager's lifecycle around the
gRPC stream (create / close / shutdown / duplicate-create).

Run-row state transitions (DONE / ERROR / STOPPED) are the daemon's
responsibility; coverage for those lives in `tests/test_actor.py`
(LocalActorService unit tests) and `tests/test_daemon_wire.py`
(daemon-side integration). This file does NOT re-test them.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from actor.service import RemoteActorService
from actor.types import ActorConfig
from actor.watch.interactive.diagnostics import DiagnosticRecorder
from actor.watch.interactive.manager import InteractiveSessionManager


_FAKES_BIN = Path(__file__).resolve().parent.parent / "e2e" / "fakes" / "bin"


@asynccontextmanager
async def running_daemon(
    tmp_dir: Path,
) -> AsyncIterator[tuple[RemoteActorService, Path]]:
    sock = tmp_dir / ".actor" / "daemon.sock"
    db = tmp_dir / ".actor" / "actor.db"
    pidfile = tmp_dir / ".actor" / "daemon.pid"
    log = tmp_dir / ".actor" / "daemon.log"
    db.parent.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "HOME": str(tmp_dir),
        "PATH": f"{_FAKES_BIN}:{os.environ.get('PATH', '')}",
        "FAKE_CLAUDE_INTERACTIVE": "1",
        "FAKE_CLAUDE_INTERACTIVE_QUIT": "BYE",
    }
    env.pop("ACTOR_NAME", None)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "actor.daemon",
        "--listen", f"unix:{sock}",
        "--db-path", str(db),
        "--pidfile", str(pidfile),
        "--log-file", str(log),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    started = False
    for _ in range(200):
        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
            raise RuntimeError(f"daemon died early: {stderr}")
        if sock.exists():
            try:
                from grpclib.client import Channel
                from actor._proto.actor.v1 import (
                    ActorExistsRequest, ActorServiceStub,
                )
                chan = Channel(path=str(sock))
                try:
                    stub = ActorServiceStub(chan)
                    await stub.actor_exists(
                        ActorExistsRequest(name="__probe__"), timeout=0.5,
                    )
                    started = True
                    break
                finally:
                    chan.close()
            except Exception:
                pass
        await asyncio.sleep(0.05)
    if not started:
        proc.terminate()
        await proc.wait()
        raise RuntimeError("actord did not bind in time")

    service = RemoteActorService(f"unix:{sock}", auto_spawn=False)
    try:
        yield service, sock
    finally:
        await service.aclose()
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def _seed_actor(client: RemoteActorService, *, name: str, dir: Path) -> None:
    """Create an actor + run it once so the daemon stamps an
    agent_session — interactive sessions need a session id."""
    await client.new_actor(
        name=name, dir=str(dir), no_worktree=True, base=None,
        agent_name="claude", config=ActorConfig(),
    )
    await client.run_actor(name=name, prompt="hi", config=ActorConfig())


class InteractiveSessionManagerWireTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="actord-mgr-")
        self.tmp = Path(self._tmp)
        self.actor_dir = self.tmp / "work"
        self.actor_dir.mkdir(parents=True)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_create_opens_remote_stream(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            await _seed_actor(client, name="alice", dir=self.actor_dir)

            recorder = DiagnosticRecorder(capacity=64)
            manager = InteractiveSessionManager(
                service_factory=lambda: client, recorder=recorder,
            )
            try:
                info = await manager.create(
                    actor_name="alice",
                    agent=None,
                    session_id="ignored",
                    cwd=self.actor_dir,
                    config=ActorConfig(),
                )
                self.assertEqual(info.actor_name, "alice")
                self.assertTrue(manager.has("alice"))
                self.assertIsNotNone(info.widget)
                # Adapter exposes pid=None on the remote path (the
                # daemon owns the PID).
                self.assertIsNone(info.session.pid)
            finally:
                await manager.close_all()

    async def test_duplicate_create_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            await _seed_actor(client, name="alice", dir=self.actor_dir)
            manager = InteractiveSessionManager(service_factory=lambda: client)
            try:
                await manager.create(
                    actor_name="alice", agent=None, session_id="x",
                    cwd=self.actor_dir, config=ActorConfig(),
                )
                with self.assertRaises(RuntimeError):
                    await manager.create(
                        actor_name="alice", agent=None, session_id="x",
                        cwd=self.actor_dir, config=ActorConfig(),
                    )
            finally:
                await manager.close_all()

    async def test_close_drops_session_from_registry(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            await _seed_actor(client, name="alice", dir=self.actor_dir)
            manager = InteractiveSessionManager(service_factory=lambda: client)
            try:
                await manager.create(
                    actor_name="alice", agent=None, session_id="x",
                    cwd=self.actor_dir, config=ActorConfig(),
                )
                self.assertTrue(manager.has("alice"))
                await manager.close("alice")
                self.assertFalse(manager.has("alice"))
            finally:
                await manager.close_all()

    async def test_shutdown_drops_every_session(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            await _seed_actor(client, name="alice", dir=self.actor_dir)
            await _seed_actor(client, name="bob", dir=self.actor_dir)
            manager = InteractiveSessionManager(service_factory=lambda: client)
            for name in ("alice", "bob"):
                await manager.create(
                    actor_name=name, agent=None, session_id="x",
                    cwd=self.actor_dir, config=ActorConfig(),
                )
            self.assertEqual(sorted(manager.live_names()), ["alice", "bob"])
            await manager.shutdown()
            self.assertEqual(manager.live_names(), [])

    async def test_local_service_factory_rejected(self) -> None:
        """Manager requires a RemoteActorService — Phase 2.5 closed
        the local fallback. Passing a local service factory fails
        loudly at create() time."""
        async with running_daemon(self.tmp) as (_client, _sock):
            from actor.db import Database
            from actor.git import RealGit
            from actor.process import RealProcessManager
            from actor.service import LocalActorService

            local = LocalActorService(
                db=Database.open(":memory:"),
                git=RealGit(),
                proc_mgr=RealProcessManager(),
            )
            manager = InteractiveSessionManager(service_factory=lambda: local)
            with self.assertRaises(RuntimeError):
                await manager.create(
                    actor_name="anything", agent=None, session_id="x",
                    cwd=self.actor_dir, config=ActorConfig(),
                )


if __name__ == "__main__":
    unittest.main()
