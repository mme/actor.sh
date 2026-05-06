"""Integration tests for the actord wire (issue #35 phase 1).

Spawns a real `python -m actor.daemon` subprocess in a temp HOME and
exercises the round-trip through `RemoteActorService`. The daemon
process is the SUT; we don't import its handler in-process.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from actor.db import Database
from actor.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    decode_message,
    encode_request,
)
from actor.service import RemoteActorService
from actor.types import Actor, ActorConfig, AgentKind, Run, Status, _now_iso


def _make_actor(
    name: str,
    *,
    agent: AgentKind = AgentKind.CLAUDE,
    dir: str = "/tmp/x",
    config: ActorConfig | None = None,
) -> Actor:
    now = _now_iso()
    return Actor(
        name=name,
        agent=agent,
        agent_session=None,
        dir=dir,
        source_repo=None,
        base_branch=None,
        worktree=False,
        parent=None,
        config=config or ActorConfig(),
        created_at=now,
        updated_at=now,
    )


def _seed_actor(db_path: Path, actor: Actor, *, run_status: Status | None = None) -> None:
    """Insert an actor (and optionally a terminal run row) directly into
    the daemon's DB before the daemon starts. Used to drive
    `list_actors` without spinning up the run lifecycle."""
    db = Database.open(str(db_path))
    try:
        db.insert_actor(actor)
        if run_status is not None:
            run = Run(
                id=0,
                actor_name=actor.name,
                prompt="seed",
                status=run_status,
                exit_code=0 if run_status == Status.DONE else None,
                pid=None,
                config=ActorConfig(),
                started_at=_now_iso(),
                finished_at=_now_iso(),
            )
            db.insert_run(run)
    finally:
        # Database has no .close()? Best-effort; daemon will reopen.
        try:
            db._conn.close()  # type: ignore[attr-defined]
        except Exception:
            pass


@asynccontextmanager
async def running_daemon(tmp_dir: Path) -> AsyncIterator[tuple[RemoteActorService, Path]]:
    """Spawn `python -m actor.daemon` against `tmp_dir` as $HOME.
    Yields a `RemoteActorService` and the socket path."""
    sock = tmp_dir / "daemon.sock"
    db = tmp_dir / ".actor" / "actor.db"
    pidfile = tmp_dir / ".actor" / "daemon.pid"
    log = tmp_dir / ".actor" / "daemon.log"
    db.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "HOME": str(tmp_dir)}
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

    # Poll until socket accepts.
    from websockets.asyncio.client import unix_connect

    started = False
    for _ in range(200):
        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
            raise RuntimeError(
                f"daemon exited early (rc={proc.returncode}): {stderr}"
            )
        if sock.exists():
            try:
                conn = await unix_connect(str(sock))
                await conn.close()
                started = True
                break
            except Exception:
                pass
        await asyncio.sleep(0.05)
    if not started:
        proc.terminate()
        await proc.wait()
        raise RuntimeError("daemon failed to start within timeout")

    try:
        yield RemoteActorService(f"unix:{sock}"), sock
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


class TestDaemonProtocol(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="actord-test-")
        self.tmp = Path(self._tmp)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_empty_db_returns_empty_list(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            actors = await client.list_actors()
        self.assertEqual(actors, [])

    async def test_populated_db_round_trips(self) -> None:
        db_path = self.tmp / ".actor" / "actor.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        a1 = _make_actor("alpha", agent=AgentKind.CLAUDE)
        a2 = _make_actor(
            "bravo",
            agent=AgentKind.CODEX,
            dir="/tmp/bravo",
            config=ActorConfig(
                actor_keys={"use-subscription": "true"},
                agent_args={"m": "o3"},
            ),
        )
        _seed_actor(db_path, a1)
        _seed_actor(db_path, a2)

        async with running_daemon(self.tmp) as (client, _sock):
            actors = await client.list_actors()

        names = sorted(a.name for a in actors)
        self.assertEqual(names, ["alpha", "bravo"])
        by_name = {a.name: a for a in actors}
        self.assertEqual(by_name["alpha"].agent, AgentKind.CLAUDE)
        self.assertEqual(by_name["bravo"].agent, AgentKind.CODEX)
        self.assertEqual(by_name["bravo"].dir, "/tmp/bravo")
        self.assertEqual(
            by_name["bravo"].config.actor_keys, {"use-subscription": "true"},
        )
        self.assertEqual(by_name["bravo"].config.agent_args, {"m": "o3"})

    async def test_status_filter_through_wire(self) -> None:
        db_path = self.tmp / ".actor" / "actor.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # idle: no runs
        _seed_actor(db_path, _make_actor("idle1"))
        # done: one DONE run
        _seed_actor(
            db_path, _make_actor("done1"), run_status=Status.DONE,
        )
        # done: another DONE run
        _seed_actor(
            db_path, _make_actor("done2"), run_status=Status.DONE,
        )

        async with running_daemon(self.tmp) as (client, _sock):
            done = await client.list_actors(status_filter="done")
            idle = await client.list_actors(status_filter="idle")

        self.assertEqual(sorted(a.name for a in done), ["done1", "done2"])
        self.assertEqual([a.name for a in idle], ["idle1"])

    async def test_method_not_found(self) -> None:
        from websockets.asyncio.client import unix_connect

        async with running_daemon(self.tmp) as (_client, sock):
            async with unix_connect(str(sock)) as ws:
                await ws.send(encode_request(JSONRPCRequest(
                    id=99, method="nope", params={},
                )))
                raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode()
        msg = decode_message(raw)
        self.assertIsInstance(msg, JSONRPCError)
        assert isinstance(msg, JSONRPCError)
        self.assertEqual(msg.code, METHOD_NOT_FOUND)
        self.assertEqual(msg.id, 99)
        self.assertIn("nope", msg.message)

    async def test_two_concurrent_clients(self) -> None:
        db_path = self.tmp / ".actor" / "actor.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _seed_actor(db_path, _make_actor("solo"))

        async with running_daemon(self.tmp) as (_client, sock):
            c1 = RemoteActorService(f"unix:{sock}")
            c2 = RemoteActorService(f"unix:{sock}")
            r1, r2 = await asyncio.gather(
                c1.list_actors(), c2.list_actors(),
            )
        self.assertEqual([a.name for a in r1], ["solo"])
        self.assertEqual([a.name for a in r2], ["solo"])

    async def test_stale_socket_cleanup(self) -> None:
        # Drop a non-listening socket file at the path the daemon will
        # bind. The daemon must detect it's not a live listener,
        # unlink it, and start cleanly.
        stale_sock = self.tmp / "daemon.sock"
        stale_sock.write_text("")
        self.assertTrue(stale_sock.exists())

        async with running_daemon(self.tmp) as (client, _sock):
            actors = await client.list_actors()
        self.assertEqual(actors, [])


if __name__ == "__main__":
    unittest.main()
