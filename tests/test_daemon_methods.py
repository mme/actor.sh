"""Wire-level round-trip tests for every method routed through actord.

Phase 2 of issue #35: covers each `ActorService` method exposed via the
daemon dispatch table, plus at least one error path per method. The
test spawns a real `python -m actor.daemon` subprocess against a temp
HOME and exercises it through `RemoteActorService` — same shape as
`tests/test_daemon_protocol.py`, just bigger surface.

Run-lifecycle tests (`start_run`, `wait_for_run`, `run_actor`,
`stop_actor`, `get_logs`) need a real agent binary; they reuse the
e2e fakes (`e2e/fakes/bin/{claude,codex}`) by prepending the fakes dir
to PATH for the daemon process.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Tuple

from actor.db import Database
from actor.errors import (
    ActorError,
    AlreadyExistsError,
    NotFoundError,
    NotRunningError,
)
from actor.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    JSONRPCError,
    JSONRPCRequest,
    decode_message,
    encode_request,
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
) -> AsyncIterator[Tuple[RemoteActorService, Path]]:
    """Spawn `python -m actor.daemon` against `tmp_dir` as $HOME.
    Yields a `RemoteActorService` and the socket path.

    `extra_path=True` prepends the e2e fakes dir to PATH so the daemon
    can spawn a fake `claude` / `codex` for run-lifecycle tests."""
    sock = tmp_dir / ".actor" / "daemon.sock"
    db = tmp_dir / ".actor" / "actor.db"
    pidfile = tmp_dir / ".actor" / "daemon.pid"
    log = tmp_dir / ".actor" / "daemon.log"
    db.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "HOME": str(tmp_dir)}
    # Drop any inherited ACTOR_NAME so the daemon's parent-resolution
    # contextvar fallback doesn't see a value from the test runner's
    # surrounding session.
    env.pop("ACTOR_NAME", None)
    if extra_path:
        env["PATH"] = f"{_FAKES_BIN}:{env.get('PATH', '')}"

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

    from websockets.asyncio.client import unix_connect as _unix_connect

    started = False
    for _ in range(200):
        if proc.returncode is not None:
            stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
            raise RuntimeError(
                f"daemon exited early (rc={proc.returncode}): {stderr}"
            )
        if sock.exists():
            try:
                conn = await _unix_connect(str(sock))
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


class WireTests(unittest.IsolatedAsyncioTestCase):
    """Each method's wire round-trip + at least one error path."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="actord-wire-")
        self.tmp = Path(self._tmp)
        self.db = self.tmp / ".actor" / "actor.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # new_actor
    # ------------------------------------------------------------------

    async def test_new_actor_round_trip(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            actor = await client.new_actor(
                name="alice",
                dir=str(self.tmp),
                no_worktree=True,
                base=None,
                agent_name="claude",
                config=ActorConfig(),
            )
        self.assertEqual(actor.name, "alice")
        self.assertEqual(actor.agent, AgentKind.CLAUDE)
        self.assertFalse(actor.worktree)

    async def test_new_actor_duplicate_raises_already_exists(self) -> None:
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

    # ------------------------------------------------------------------
    # discard_actor
    # ------------------------------------------------------------------

    async def test_discard_actor_round_trip(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            result = await client.discard_actor("alice", force=True)
            self.assertEqual(result.names, ["alice"])
            self.assertEqual(await client.list_actors(), [])

    async def test_discard_unknown_actor_raises_not_found(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.discard_actor("ghost")

    # ------------------------------------------------------------------
    # config_actor
    # ------------------------------------------------------------------

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

    async def test_config_actor_unknown_actor_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.config_actor("ghost")

    # ------------------------------------------------------------------
    # start_run / wait_for_run / run_actor / stop_actor
    # ------------------------------------------------------------------

    async def test_run_actor_round_trip(self) -> None:
        # Real daemon + fake claude. Daemon spawns the fake via PATH,
        # which echoes the prompt back.
        actor_dir = self.tmp / "work"
        actor_dir.mkdir()
        async with running_daemon(self.tmp, extra_path=True) as (client, _sock):
            await client.new_actor(
                name="alice",
                dir=str(actor_dir),
                no_worktree=True,
                base=None,
                agent_name="claude",
                config=ActorConfig(),
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
                name="alice",
                dir=str(actor_dir),
                no_worktree=True,
                base=None,
                agent_name="claude",
                config=ActorConfig(),
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

    async def test_stop_actor_when_idle_raises_not_running(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotRunningError):
                await client.stop_actor("alice")

    # ------------------------------------------------------------------
    # Discovery: get_actor / actor_exists / actor_status / latest_run /
    # show_actor / list_runs / get_run
    # ------------------------------------------------------------------

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

    async def test_actor_status_invalid_filter_raises(self) -> None:
        # actor_status itself doesn't raise on unknown name (returns
        # IDLE) — but list_actors does validate the filter, which
        # exercises the same wire-error path.
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(ActorError):
                await client.list_actors(status_filter="not-a-status")

    async def test_latest_run_returns_none_for_idle(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            self.assertIsNone(await client.latest_run("alice"))

    async def test_latest_run_returns_run_when_present(self) -> None:
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
        self.assertEqual(run.actor_name, "alice")
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
            # Inspect the seeded run id via list_runs.
            runs, _ = await client.list_runs("alice", limit=1)
            assert runs
            run = await client.get_run(runs[0].id)
        assert run is not None
        self.assertEqual(run.prompt, "task")

    async def test_get_run_unknown_returns_none(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            self.assertIsNone(await client.get_run(99999))

    # ------------------------------------------------------------------
    # get_logs
    # ------------------------------------------------------------------

    async def test_get_logs_idle_actor_returns_empty(self) -> None:
        _seed(self.db, _make_actor("alice"))
        async with running_daemon(self.tmp) as (client, _sock):
            logs = await client.get_logs("alice")
        self.assertIsNone(logs.session_id)
        self.assertEqual(logs.entries, [])

    async def test_get_logs_unknown_actor_raises(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            with self.assertRaises(NotFoundError):
                await client.get_logs("ghost")

    # ------------------------------------------------------------------
    # list_roles
    # ------------------------------------------------------------------

    async def test_list_roles_includes_built_in_main(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            roles = await client.list_roles()
        self.assertIn("main", roles)
        self.assertEqual(roles["main"].agent, "claude")

    async def test_list_roles_picks_up_user_settings(self) -> None:
        # Project settings load relative to caller cwd; user settings
        # come from $HOME unconditionally.
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

    # ------------------------------------------------------------------
    # publish_notification
    # ------------------------------------------------------------------

    async def test_publish_notification_round_trip(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            sub = RemoteActorService(f"unix:{_sock}")
            seen: list[Notification] = []
            cancel = await sub.subscribe_notifications(seen.append)
            try:
                await asyncio.sleep(0.05)
                await client.publish_notification(Notification(
                    actor="alice", event="run_completed",
                    run_id=1, status=Status.DONE, output="hi",
                ))
                # Give the fan-out a tick.
                await asyncio.sleep(0.2)
            finally:
                cancel()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].actor, "alice")
        self.assertEqual(seen[0].status, Status.DONE)

    async def test_publish_notification_invalid_payload_raises(self) -> None:
        async with running_daemon(self.tmp) as (_client, sock):
            from websockets.asyncio.client import unix_connect
            async with unix_connect(str(sock)) as ws:
                await ws.send(encode_request(JSONRPCRequest(
                    id=1, method="publish_notification",
                    params={"notification": "not-a-dict"},
                )))
                raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode()
        msg = decode_message(raw)
        self.assertIsInstance(msg, JSONRPCError)
        assert isinstance(msg, JSONRPCError)
        self.assertEqual(msg.code, INVALID_PARAMS)

    # ------------------------------------------------------------------
    # subscribe_notifications
    # ------------------------------------------------------------------

    async def test_subscribe_then_cancel_drops_subscription(self) -> None:
        async with running_daemon(self.tmp) as (client, _sock):
            seen: list[Notification] = []
            cancel = await client.subscribe_notifications(seen.append)
            await asyncio.sleep(0.05)
            cancel()
            # Give cancel time to take effect.
            await asyncio.sleep(0.2)

            pub = RemoteActorService(f"unix:{_sock}")
            await pub.publish_notification(Notification(
                actor="alice", event="run_completed",
                run_id=1, status=Status.DONE,
            ))
            await asyncio.sleep(0.2)
        self.assertEqual(seen, [])


class WireDaemonUnreachableTests(unittest.IsolatedAsyncioTestCase):
    """RemoteActorService should raise a clear error when actord
    isn't running — same friendly message the CLI surfaces."""

    async def test_call_against_missing_socket_raises(self) -> None:
        from actor.errors import DaemonUnreachableError
        client = RemoteActorService("unix:/nonexistent/path/sock")
        with self.assertRaises(DaemonUnreachableError):
            await client.list_actors()

    async def test_subscribe_against_missing_socket_raises(self) -> None:
        from actor.errors import DaemonUnreachableError
        client = RemoteActorService("unix:/nonexistent/path/sock")
        with self.assertRaises(DaemonUnreachableError):
            await client.subscribe_notifications(lambda _n: None)


if __name__ == "__main__":
    unittest.main()
