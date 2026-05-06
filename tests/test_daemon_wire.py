"""Wire-level round-trip tests for every method routed through actord.

Phase 2.5 of issue #35: gRPC migration. Every test exercises
`RemoteActorService.method(...)` against a real `python -m
actor.daemon` subprocess, plus daemon-unreachable behavior. Replaces
the Phase-2 JSON-RPC tests in `test_daemon_methods.py` and
`test_daemon_protocol.py` with the same semantic coverage on the new
wire — request/response framing details are owned by gRPC + the
generated stubs, so the assertions stay focused on observable
behavior.

Run-lifecycle tests (`start_run`, `wait_for_run`, `run_actor`,
`stop_actor`, `interactive_session`, `get_logs`) use the e2e fakes
(`e2e/fakes/bin/{claude,codex}`) by prepending the fakes dir to PATH
for the daemon process.
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
from typing import AsyncIterator, Tuple

from actor.db import Database
from actor.errors import (
    ActorError,
    AlreadyExistsError,
    DaemonUnreachableError,
    NotFoundError,
    NotRunningError,
)
from actor.service import Notification, RemoteActorService
from actor.types import (
    Actor,
    ActorConfig,
    AgentKind,
    Run,
    Status,
    _now_iso,
)


_FAKES_BIN = Path(__file__).resolve().parent.parent / "e2e" / "fakes" / "bin"


def _make_actor(
    name: str,
    *,
    agent: AgentKind = AgentKind.CLAUDE,
    dir: str = "/tmp/x",
    session: str | None = None,
    parent: str | None = None,
    config: ActorConfig | None = None,
) -> Actor:
    now = _now_iso()
    return Actor(
        name=name,
        agent=agent,
        agent_session=session,
        dir=dir,
        source_repo=None,
        base_branch=None,
        worktree=False,
        parent=parent,
        config=config or ActorConfig(),
        created_at=now,
        updated_at=now,
    )


def _seed(
    db_path: Path,
    actor: Actor,
    *,
    runs: list[Run] | None = None,
) -> None:
    db = Database.open(str(db_path))
    try:
        db.insert_actor(actor)
        for run in runs or []:
            db.insert_run(run)
    finally:
        try:
            db._conn.close()  # type: ignore[attr-defined]
        except Exception:
            pass


@asynccontextmanager
async def running_daemon(
    tmp_dir: Path,
    *,
    extra_path: bool = False,
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[Tuple[RemoteActorService, Path]]:
    sock = tmp_dir / ".actor" / "daemon.sock"
    db = tmp_dir / ".actor" / "actor.db"
    pidfile = tmp_dir / ".actor" / "daemon.pid"
    log = tmp_dir / ".actor" / "daemon.log"
    db.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "HOME": str(tmp_dir)}
    env.pop("ACTOR_NAME", None)
    if extra_path:
        env["PATH"] = f"{_FAKES_BIN}:{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)

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

    # Probe with a real gRPC `actor_exists` call.
    started = False
    for _ in range(200):
        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
            raise RuntimeError(
                f"daemon exited early (rc={proc.returncode}): {stderr}"
            )
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


class WireTests(unittest.IsolatedAsyncioTestCase):
    """Each method's wire round-trip + at least one error path."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="actord-wire-")
        self.tmp = Path(self._tmp)
        self.db = self.tmp / ".actor" / "actor.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- new_actor ----------------------------------------------------

    async def test_new_actor_round_trip(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            actor = await client.new_actor(
                name="alice", dir=str(self.tmp), no_worktree=True,
                base=None, agent_name="claude", config=ActorConfig(),
            )
        self.assertEqual(actor.name, "alice")
        self.assertEqual(actor.agent, AgentKind.CLAUDE)
        self.assertFalse(actor.worktree)

    async def test_new_actor_duplicate_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            await client.new_actor(
                name="alice", dir=str(self.tmp), no_worktree=True,
                base=None, agent_name="claude", config=ActorConfig(),
            )
            with self.assertRaises(AlreadyExistsError):
                await client.new_actor(
                    name="alice", dir=str(self.tmp), no_worktree=True,
                    base=None, agent_name="claude", config=ActorConfig(),
                )

    # -- discard_actor ------------------------------------------------

    async def test_discard_actor_round_trip(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            result = await client.discard_actor("alice", force=True)
            self.assertEqual(result.names, ["alice"])
            self.assertEqual(await client.list_actors(), [])

    async def test_discard_unknown_actor_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.discard_actor("ghost")

    # -- config_actor -------------------------------------------------

    async def test_config_actor_view_and_update_round_trip(self) -> None:
        _seed(self.db, _make_actor(
            "alice",
            config=ActorConfig(agent_args={"model": "sonnet"}),
        ))
        async with running_daemon(self.tmp) as (client, _sock):
            cfg = await client.config_actor("alice")
            self.assertEqual(cfg.agent_args, {"model": "sonnet"})

            updated = await client.config_actor("alice", pairs=["model=opus"])
            self.assertEqual(updated.agent_args, {"model": "opus"})

    async def test_config_actor_unknown_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.config_actor("ghost")

    # -- run lifecycle ------------------------------------------------

    async def test_run_actor_round_trip(self) -> None:
        actor_dir = self.tmp / "work"
        actor_dir.mkdir()
        async with running_daemon(self.tmp, extra_path=True) as (client, _sock):
            await client.new_actor(
                name="alice", dir=str(actor_dir), no_worktree=True,
                base=None, agent_name="claude", config=ActorConfig(),
            )
            result = await client.run_actor(
                name="alice", prompt="hello", config=ActorConfig(),
            )
            self.assertEqual(result.actor, "alice")
            self.assertEqual(result.status, Status.DONE)
            self.assertEqual(result.exit_code, 0)

    async def test_run_actor_unknown_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.run_actor(
                    name="ghost", prompt="hi", config=ActorConfig(),
                )

    async def test_start_run_then_wait_for_run(self) -> None:
        actor_dir = self.tmp / "work"
        actor_dir.mkdir()
        async with running_daemon(self.tmp, extra_path=True) as (client, _sock):
            await client.new_actor(
                name="alice", dir=str(actor_dir), no_worktree=True,
                base=None, agent_name="claude", config=ActorConfig(),
            )
            handle = await client.start_run(
                name="alice", prompt="hi", config=ActorConfig(),
            )
            self.assertEqual(handle.status, Status.RUNNING)
            self.assertIsNotNone(handle.pid)

            result = await client.wait_for_run(handle.run_id)
            self.assertEqual(result.status, Status.DONE)

    async def test_wait_for_run_unknown_id_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(ActorError):
                await client.wait_for_run(99999)

    async def test_stop_actor_when_idle_raises(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotRunningError):
                await client.stop_actor("alice")

    # -- discovery ----------------------------------------------------

    async def test_get_actor_round_trip(self) -> None:
        _seed(self.db, _make_actor("alice", session="sid-1"))
        async with running_daemon(self.tmp) as (client, _sock):
            got = await client.get_actor("alice")
        self.assertEqual(got.name, "alice")
        self.assertEqual(got.agent_session, "sid-1")

    async def test_get_actor_unknown_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.get_actor("ghost")

    async def test_actor_exists_round_trip(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            self.assertTrue(await client.actor_exists("alice"))
            self.assertFalse(await client.actor_exists("ghost"))

    async def test_list_actors_empty(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            actors = await client.list_actors()
        self.assertEqual(actors, [])

    async def test_list_actors_populated(self) -> None:
        _seed(self.db, _make_actor("alpha"))
        _seed(self.db, _make_actor(
            "bravo", agent=AgentKind.CODEX, dir="/tmp/bravo",
            config=ActorConfig(actor_keys={"use-subscription": "true"}),
        ))
        async with running_daemon(self.tmp) as (client, _sock):
            actors = await client.list_actors()
        names = sorted(a.name for a in actors)
        self.assertEqual(names, ["alpha", "bravo"])
        bravo = next(a for a in actors if a.name == "bravo")
        self.assertEqual(bravo.agent, AgentKind.CODEX)
        self.assertEqual(bravo.config.actor_keys, {"use-subscription": "true"})

    async def test_list_actors_status_filter(self) -> None:
        _seed(self.db, _make_actor("idle1"))
        _seed(
            self.db, _make_actor("done1"),
            runs=[Run(
                id=0, actor_name="done1", prompt="x",
                status=Status.DONE, exit_code=0, pid=None,
                config=ActorConfig(),
                started_at=_now_iso(), finished_at=_now_iso(),
            )],
        )
        async with running_daemon(self.tmp) as (client, _sock):
            done = await client.list_actors(status_filter="done")
            idle = await client.list_actors(status_filter="idle")
        self.assertEqual([a.name for a in done], ["done1"])
        self.assertEqual([a.name for a in idle], ["idle1"])

    async def test_list_actors_invalid_filter_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(ActorError):
                await client.list_actors(status_filter="not-a-status")

    async def test_actor_status_round_trip(self) -> None:
        _seed(
            self.db, _make_actor("alice"),
            runs=[Run(
                id=0, actor_name="alice", prompt="x",
                status=Status.DONE, exit_code=0, pid=None,
                config=ActorConfig(),
                started_at=_now_iso(), finished_at=_now_iso(),
            )],
        )
        async with running_daemon(self.tmp) as (client, _sock):
            status = await client.actor_status("alice")
        self.assertEqual(status, Status.DONE)

    async def test_latest_run_idle_returns_none(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            self.assertIsNone(await client.latest_run("alice"))

    async def test_latest_run_returns_run(self) -> None:
        _seed(
            self.db, _make_actor("alice"),
            runs=[Run(
                id=0, actor_name="alice", prompt="task",
                status=Status.DONE, exit_code=0, pid=42,
                config=ActorConfig(agent_args={"model": "opus"}),
                started_at=_now_iso(), finished_at=_now_iso(),
            )],
        )
        async with running_daemon(self.tmp) as (client, _sock):
            run = await client.latest_run("alice")
        assert run is not None
        self.assertEqual(run.prompt, "task")
        self.assertEqual(run.config.agent_args, {"model": "opus"})

    async def test_show_actor_round_trip(self) -> None:
        _seed(
            self.db, _make_actor("alice"),
            runs=[Run(
                id=0, actor_name="alice", prompt="task",
                status=Status.DONE, exit_code=0, pid=42,
                config=ActorConfig(),
                started_at=_now_iso(), finished_at=_now_iso(),
            )],
        )
        async with running_daemon(self.tmp) as (client, _sock):
            detail = await client.show_actor("alice", runs_limit=5)
        self.assertEqual(detail.actor.name, "alice")
        self.assertEqual(detail.total_runs, 1)
        self.assertEqual(len(detail.runs), 1)
        self.assertEqual(detail.runs_limit, 5)

    async def test_show_actor_unknown_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.show_actor("ghost")

    async def test_list_runs_round_trip(self) -> None:
        _seed(
            self.db, _make_actor("alice"),
            runs=[
                Run(
                    id=0, actor_name="alice", prompt=f"r{i}",
                    status=Status.DONE, exit_code=0, pid=None,
                    config=ActorConfig(),
                    started_at=_now_iso(), finished_at=_now_iso(),
                )
                for i in range(3)
            ],
        )
        async with running_daemon(self.tmp) as (client, _sock):
            runs, total = await client.list_runs("alice", limit=2)
        self.assertEqual(total, 3)
        self.assertEqual(len(runs), 2)

    async def test_list_runs_unknown_actor_returns_empty(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            runs, total = await client.list_runs("ghost", limit=5)
        self.assertEqual(runs, [])
        self.assertEqual(total, 0)

    async def test_get_run_round_trip(self) -> None:
        _seed(
            self.db, _make_actor("alice"),
            runs=[Run(
                id=0, actor_name="alice", prompt="task",
                status=Status.DONE, exit_code=0, pid=42,
                config=ActorConfig(),
                started_at=_now_iso(), finished_at=_now_iso(),
            )],
        )
        async with running_daemon(self.tmp) as (client, _sock):
            runs, _ = await client.list_runs("alice", limit=1)
            assert runs
            run = await client.get_run(runs[0].id)
        assert run is not None
        self.assertEqual(run.prompt, "task")

    async def test_get_run_unknown_returns_none(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            self.assertIsNone(await client.get_run(99999))

    async def test_get_logs_idle_returns_empty(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            logs = await client.get_logs("alice")
        self.assertIsNone(logs.session_id)
        self.assertEqual(logs.entries, [])

    async def test_get_logs_unknown_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.get_logs("ghost")

    async def test_list_roles_includes_main(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            roles = await client.list_roles()
        self.assertIn("main", roles)
        self.assertEqual(roles["main"].agent, "claude")

    async def test_list_roles_picks_up_user_settings(self) -> None:
        settings = self.tmp / ".actor" / "settings.kdl"
        settings.write_text(
            'role "qa" {\n'
            '    agent "claude"\n'
            '    description "QA reviewer"\n'
            '}\n'
        )
        async with running_daemon(self.tmp) as (client, _sock):
            roles = await client.list_roles()
        self.assertIn("qa", roles)
        self.assertEqual(roles["qa"].description, "QA reviewer")

    # -- notifications ------------------------------------------------

    async def test_publish_then_subscriber_receives(self) -> None:
        async with running_daemon(self.tmp) as (client, sock):
            sub = RemoteActorService(f"unix:{sock}")
            seen: list[Notification] = []
            cancel = await sub.subscribe_notifications(seen.append)
            try:
                await asyncio.sleep(0.1)
                await client.publish_notification(Notification(
                    actor="alice", event="run_completed",
                    run_id=1, status=Status.DONE, output="hi",
                ))
                await asyncio.sleep(0.3)
            finally:
                cancel()
                await asyncio.sleep(0.1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].actor, "alice")
        self.assertEqual(seen[0].status, Status.DONE)

    async def test_subscribe_then_cancel_drops(self) -> None:
        async with running_daemon(self.tmp) as (client, sock):
            seen: list[Notification] = []
            cancel = await client.subscribe_notifications(seen.append)
            await asyncio.sleep(0.05)
            cancel()
            await asyncio.sleep(0.2)

            pub = RemoteActorService(f"unix:{sock}")
            await pub.publish_notification(Notification(
                actor="alice", event="run_completed",
                run_id=1, status=Status.DONE,
            ))
            await asyncio.sleep(0.2)
        self.assertEqual(seen, [])


class WireDaemonUnreachableTests(unittest.IsolatedAsyncioTestCase):
    """RemoteActorService surfaces a clear error when actord isn't
    running — mirrors the Phase 2 contract."""

    async def test_call_against_missing_socket_raises(self) -> None:
        client = RemoteActorService("unix:/nonexistent/path/sock")
        with self.assertRaises(DaemonUnreachableError):
            await client.list_actors()

    async def test_subscribe_against_missing_socket_raises(self) -> None:
        client = RemoteActorService("unix:/nonexistent/path/sock")
        with self.assertRaises(DaemonUnreachableError):
            await client.subscribe_notifications(lambda _n: None)


# ---------------------------------------------------------------------------
# InteractiveSession bidirectional streaming
# ---------------------------------------------------------------------------


class InteractiveSessionWireTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests for the bidi `InteractiveSession` RPC.

    Spawns a daemon with the e2e fakes on PATH; the fake claude reads
    its FAKE_CLAUDE_INTERACTIVE env var and acts as a tiny echo shell.
    Each test exercises a different facet of the wire — open, stdin
    round-trip, exit info, premature client close.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="actord-int-")
        self.tmp = Path(self._tmp)
        (self.tmp / ".actor").mkdir()
        self.actor_dir = self.tmp / "work"
        self.actor_dir.mkdir()

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    @asynccontextmanager
    async def _daemon_with_seed(
        self, *, fake_env: dict[str, str] | None = None,
    ) -> AsyncIterator[RemoteActorService]:
        env = {
            "FAKE_CLAUDE_INTERACTIVE": "1",
            "FAKE_CLAUDE_INTERACTIVE_QUIT": "BYE",
        }
        if fake_env:
            env.update(fake_env)
        async with running_daemon(
            self.tmp, extra_path=True, extra_env=env,
        ) as (client, _sock):
            # Seed an actor + first run so an agent_session lands in the DB.
            await client.new_actor(
                name="alice", dir=str(self.actor_dir), no_worktree=True,
                base=None, agent_name="claude", config=ActorConfig(),
            )
            await client.run_actor(
                name="alice", prompt="hi", config=ActorConfig(),
            )
            yield client

    async def test_open_then_stdin_echoes_and_exits(self) -> None:
        async with self._daemon_with_seed() as client:
            collected = b""
            async with client.interactive_session(
                "alice", cols=80, rows=24,
            ) as session:
                async def reader():
                    nonlocal collected
                    while True:
                        f = await session.recv()
                        if f is None or f.kind == "exit":
                            return
                        if f.kind == "stdout":
                            collected += f.stdout or b""
                t = asyncio.create_task(reader())
                await asyncio.sleep(0.4)
                await session.send_stdin(b"hello\n")
                await asyncio.sleep(0.2)
                await session.send_stdin(b"BYE\n")
                await asyncio.wait_for(t, timeout=5)
                ec = await session.exit_code()

        self.assertEqual(ec, 0)
        self.assertIn(b"hello", collected)
        self.assertIn(b"BYE", collected)

    async def test_exit_info_carries_final_status(self) -> None:
        async with self._daemon_with_seed() as client:
            async with client.interactive_session("alice") as session:
                async def drain():
                    while True:
                        f = await session.recv()
                        if f is None or f.kind == "exit":
                            return
                t = asyncio.create_task(drain())
                await asyncio.sleep(0.4)
                await session.send_stdin(b"BYE\n")
                await asyncio.wait_for(t, timeout=5)
                ec = await session.exit_code()
                self.assertEqual(ec, 0)
                # final_status should be DONE for a clean exit.
                self.assertEqual(session.final_status, Status.DONE)

    async def test_resize_does_not_crash(self) -> None:
        # We can't easily observe the SIGWINCH on the child from here,
        # but we can confirm the resize ClientFrame doesn't disrupt the
        # bidi stream.
        async with self._daemon_with_seed() as client:
            async with client.interactive_session("alice") as session:
                async def drain():
                    while True:
                        f = await session.recv()
                        if f is None or f.kind == "exit":
                            return
                t = asyncio.create_task(drain())
                await asyncio.sleep(0.4)
                await session.send_resize(100, 30)
                await asyncio.sleep(0.1)
                await session.send_stdin(b"BYE\n")
                await asyncio.wait_for(t, timeout=5)
                self.assertEqual(await session.exit_code(), 0)

    async def test_unknown_actor_raises(self) -> None:
        async with running_daemon(self.tmp, extra_path=True) as (client, _sock):
            with self.assertRaises(ActorError):
                async with client.interactive_session("ghost") as session:
                    # Attempt to recv() so the daemon's
                    # start_interactive_run actually runs.
                    while True:
                        f = await session.recv()
                        if f is None:
                            break

    async def test_premature_client_close_finalizes_run(self) -> None:
        async with self._daemon_with_seed(
            # Long-running echo so the close races the child.
            fake_env={"FAKE_CLAUDE_INTERACTIVE_QUIT": "NEVER"},
        ) as client:
            async with client.interactive_session("alice") as session:
                async def drain():
                    while True:
                        f = await session.recv()
                        if f is None or f.kind == "exit":
                            return
                t = asyncio.create_task(drain())
                await asyncio.sleep(0.4)
                await session.send_signal(15)  # SIGTERM
                # The daemon SIGTERMs the child and sends ExitInfo.
                await asyncio.wait_for(t, timeout=5)
                ec = await session.exit_code()
                # SIGTERM → -15 from waitpid path.
                self.assertLess(ec, 0)


if __name__ == "__main__":
    unittest.main()
