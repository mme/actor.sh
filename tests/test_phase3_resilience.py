"""Phase 3 resilience tests (issue #35 stage 5+6).

Exercises the auto-spawn / channel-reuse / auto-reconnect / lifecycle /
orphan-sweep / log-rotation pieces. Most tests run a real `actor`
binary in a tempdir HOME so they cover the user-facing surface end
to end; a few unit-level tests target the in-process pieces that
don't need a daemon subprocess.

Daemon hygiene: every test cleans up its daemon at teardown.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional

from actor.bootstrap import is_pid_alive, read_daemon_pid
from actor.db import Database
from actor.errors import (
    DaemonUnreachableError,
    InteractiveSessionEnded,
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


def _run_actor_cli(home: Path, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Invoke `actor <args...>` against an isolated HOME. Returns
    captured stdout/stderr + returncode."""
    env = {**os.environ, "HOME": str(home)}
    env.pop("ACTOR_NAME", None)
    return subprocess.run(
        ["actor", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _stop_daemon_in(home: Path) -> None:
    """Best-effort shutdown of any daemon still running for `home`.
    Silent on failure — used in tearDown so a leftover daemon
    doesn't poison the next test."""
    pid = read_daemon_pid(home / ".actor" / "daemon.pid")
    if pid is not None and is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not is_pid_alive(pid):
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Channel reuse
# ---------------------------------------------------------------------------


class ChannelReuseTests(unittest.IsolatedAsyncioTestCase):
    """One Channel per RemoteActorService, reused across calls."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p3-chan-"))
        (self._tmp / ".actor").mkdir()

    def tearDown(self) -> None:
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_one_channel_reused_for_many_calls(self) -> None:
        # Auto-spawn brings up the daemon for this test's HOME.
        sock = self._tmp / ".actor" / "daemon.sock"
        env = {**os.environ, "HOME": str(self._tmp)}
        env.pop("ACTOR_NAME", None)
        proc = subprocess.Popen(
            ["actor", "daemon", "start", "--foreground"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait for socket.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sock.exists():
            time.sleep(0.05)

        try:
            svc = RemoteActorService(f"unix:{sock}", auto_spawn=False)
            try:
                # Channel hasn't been opened yet.
                self.assertIsNone(svc._chan)
                # First call opens it.
                await svc.list_actors()
                first_chan = svc._chan
                self.assertIsNotNone(first_chan)
                # Subsequent calls reuse the same channel object.
                for _ in range(3):
                    await svc.list_actors()
                self.assertIs(svc._chan, first_chan)
            finally:
                await svc.aclose()
                # After aclose, channel is None and further calls fail.
                self.assertIsNone(svc._chan)
                with self.assertRaises(RuntimeError):
                    await svc.list_actors()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# Auto-spawn
# ---------------------------------------------------------------------------


class AutoSpawnTests(unittest.TestCase):
    """`actor list` (or any non-interactive command) brings the
    daemon up transparently when no daemon is running."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p3-auto-"))
        (self._tmp / ".actor").mkdir()

    def tearDown(self) -> None:
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_actor_list_auto_spawns_with_stderr_notice(self) -> None:
        # No daemon running; pidfile + socket absent.
        self.assertFalse((self._tmp / ".actor" / "daemon.pid").exists())
        self.assertFalse((self._tmp / ".actor" / "daemon.sock").exists())

        r = _run_actor_cli(self._tmp, "list")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # Exactly the one-line stderr notice; no traceback noise.
        self.assertIn("starting actord", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        # Empty actor list landed on stdout.
        self.assertIn("NAME", r.stdout)
        # And the daemon is now running.
        pidfile = self._tmp / ".actor" / "daemon.pid"
        self.assertTrue(pidfile.exists())
        pid = int(pidfile.read_text().strip())
        self.assertTrue(is_pid_alive(pid))


# ---------------------------------------------------------------------------
# Auto-reconnect for SubscribeNotifications
# ---------------------------------------------------------------------------


class AutoReconnectTests(unittest.IsolatedAsyncioTestCase):
    """Subscribe stream resumes transparently after a daemon bounce."""

    async def asyncSetUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p3-reconn-"))
        (self._tmp / ".actor").mkdir()

    async def asyncTearDown(self) -> None:
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_subscribe_resumes_after_daemon_restart(self) -> None:
        sock = self._tmp / ".actor" / "daemon.sock"
        env = {**os.environ, "HOME": str(self._tmp)}
        env.pop("ACTOR_NAME", None)

        def _accepts() -> bool:
            import socket as _s
            s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
            s.settimeout(0.1)
            try:
                s.connect(str(sock))
                return True
            except OSError:
                return False
            finally:
                try:
                    s.close()
                except OSError:
                    pass

        async def _start() -> subprocess.Popen:
            proc = subprocess.Popen(
                ["actor", "daemon", "start", "--foreground"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(200):
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"daemon exited (rc={proc.returncode}) before binding",
                    )
                if _accepts():
                    return proc
                await asyncio.sleep(0.05)
            proc.terminate()
            raise RuntimeError("daemon failed to start within 10s")

        proc = await _start()

        svc = RemoteActorService(f"unix:{sock}", auto_spawn=False)
        events: list[Notification] = []
        cancel = await svc.subscribe_notifications(events.append)
        try:
            # Sanity: a published event reaches the subscriber.
            pub = RemoteActorService(f"unix:{sock}", auto_spawn=False)
            await pub.publish_notification(Notification(
                actor="alice", event="run_completed",
                run_id=1, status=Status.DONE, output="first",
            ))
            await asyncio.sleep(0.2)
            await pub.aclose()
            self.assertEqual(len(events), 1)

            # Bounce the daemon hard. SIGKILL simulates a crash;
            # the subscribe stream breaks abruptly and the client's
            # reconnect loop reopens it. SIGTERM would also work but
            # gives the daemon up to 5s to drain — slows the test
            # without exercising additional code paths.
            proc.kill()
            proc.wait(timeout=5)

            proc = await _start()
            # Give the reconnect loop a moment.
            await asyncio.sleep(0.5)

            pub2 = RemoteActorService(f"unix:{sock}", auto_spawn=False)
            await pub2.publish_notification(Notification(
                actor="alice", event="run_completed",
                run_id=2, status=Status.DONE, output="after-bounce",
            ))
            await asyncio.sleep(0.5)
            await pub2.aclose()
        finally:
            cancel()
            await asyncio.sleep(0.1)
            await svc.aclose()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Two events overall; the second one came in after the
        # daemon was restarted.
        self.assertEqual(len(events), 2)
        outputs = [e.output for e in events]
        self.assertIn("first", outputs)
        self.assertIn("after-bounce", outputs)


# ---------------------------------------------------------------------------
# Lifecycle commands
# ---------------------------------------------------------------------------


class DaemonLifecycleTests(unittest.TestCase):
    """`actor daemon {start,stop,restart,status,logs}` produce the
    documented behavior."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p3-life-"))
        (self._tmp / ".actor").mkdir()

    def tearDown(self) -> None:
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_status_when_not_running(self) -> None:
        r = _run_actor_cli(self._tmp, "daemon", "status")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(r.stdout.strip(), "actord not running")

    def test_start_then_status_then_stop(self) -> None:
        r = _run_actor_cli(self._tmp, "daemon", "start")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("actord started", r.stdout)

        r = _run_actor_cli(self._tmp, "daemon", "status")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("actord running", r.stdout)
        self.assertIn("PID:", r.stdout)
        self.assertIn("Socket:", r.stdout)
        self.assertIn("Version:", r.stdout)
        self.assertIn("Uptime:", r.stdout)
        self.assertIn("Connections:", r.stdout)

        r = _run_actor_cli(self._tmp, "daemon", "stop")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("actord stopped", r.stdout)

        # Idempotent — second stop is a no-op.
        r = _run_actor_cli(self._tmp, "daemon", "stop")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("not running", r.stdout)

    def test_restart_bounces_pid(self) -> None:
        r1 = _run_actor_cli(self._tmp, "daemon", "start")
        self.assertEqual(r1.returncode, 0, msg=r1.stderr)
        pid1 = read_daemon_pid(self._tmp / ".actor" / "daemon.pid")
        self.assertIsNotNone(pid1)

        r2 = _run_actor_cli(self._tmp, "daemon", "restart")
        self.assertEqual(r2.returncode, 0, msg=r2.stderr)
        pid2 = read_daemon_pid(self._tmp / ".actor" / "daemon.pid")
        self.assertIsNotNone(pid2)
        self.assertNotEqual(pid1, pid2)

    def test_logs_tails_daemon_log(self) -> None:
        r = _run_actor_cli(self._tmp, "daemon", "start")
        self.assertEqual(r.returncode, 0, msg=r.stderr)

        r = _run_actor_cli(self._tmp, "daemon", "logs", "-n", "5")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # The startup line is the first thing the daemon emits.
        self.assertIn("actord listening", r.stdout)


# ---------------------------------------------------------------------------
# Crash-recovery sweep
# ---------------------------------------------------------------------------


class OrphanSweepTests(unittest.IsolatedAsyncioTestCase):
    """A `runs` row in `running` state with a dead PID is marked
    `error/-1` when the daemon starts."""

    async def asyncSetUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p3-sweep-"))
        (self._tmp / ".actor").mkdir()

    async def asyncTearDown(self) -> None:
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_running_row_with_dead_pid_marked_error(self) -> None:
        db_path = self._tmp / ".actor" / "actor.db"

        # Pre-populate: actor + a running run with a guaranteed-dead PID.
        db = Database.open(str(db_path))
        try:
            now = _now_iso()
            db.insert_actor(Actor(
                name="alice", agent=AgentKind.CLAUDE,
                agent_session=None, dir="/tmp", source_repo=None,
                base_branch=None, worktree=False, parent=None,
                config=ActorConfig(), created_at=now, updated_at=now,
            ))
            # PID 1 is init — guaranteed alive but not OUR child, so
            # `os.kill(1, 0)` succeeds. Use a high PID we own that's
            # provably dead instead.
            spawn = subprocess.Popen([sys.executable, "-c", "pass"])
            spawn.wait()
            dead_pid = spawn.pid
            db.insert_run(Run(
                id=0, actor_name="alice", prompt="orphan",
                status=Status.RUNNING, exit_code=None, pid=dead_pid,
                config=ActorConfig(),
                started_at=now, finished_at=None,
            ))
            self.assertFalse(is_pid_alive(dead_pid))
        finally:
            try:
                db._conn.close()  # type: ignore[attr-defined]
            except Exception:
                pass

        # Start the daemon and let the sweep run.
        sock = self._tmp / ".actor" / "daemon.sock"
        env = {**os.environ, "HOME": str(self._tmp)}
        env.pop("ACTOR_NAME", None)
        proc = subprocess.Popen(
            ["actor", "daemon", "start", "--foreground"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if sock.exists():
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail("daemon failed to start")

            # Inspect the row directly — the sweep should have run.
            db = Database.open(str(db_path))
            try:
                run = db.latest_run("alice")
                assert run is not None
                self.assertEqual(run.status, Status.ERROR)
                self.assertEqual(run.exit_code, -1)
            finally:
                try:
                    db._conn.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

            # And the daemon log mentions the orphan.
            log = (self._tmp / ".actor" / "daemon.log").read_text()
            self.assertIn(f"orphaned run", log)
            self.assertIn("alice", log)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------


class LogRotationTests(unittest.TestCase):
    """RotatingFileHandler creates daemon.log.1 once daemon.log
    crosses 10MB."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p3-logs-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_rotates_after_10mb(self) -> None:
        # Smoke the handler in isolation rather than driving 10MB
        # through the daemon — the rotation policy is what we want
        # to lock down, not the daemon's logging rate.
        from logging.handlers import RotatingFileHandler
        import logging

        log_file = self._tmp / "daemon.log"
        handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=3,
        )
        logger = logging.getLogger("phase3-rotation-smoke")
        for h in list(logger.handlers):
            logger.removeHandler(h)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # 12MB of payload — plenty to trigger one rotation.
        payload = "x" * 1024  # 1KB per emit
        try:
            for _ in range(12 * 1024):
                logger.info(payload)
        finally:
            handler.close()
            logger.removeHandler(handler)

        rotated = self._tmp / "daemon.log.1"
        self.assertTrue(
            rotated.exists(),
            f"expected {rotated} to exist after >10MB; "
            f"got {sorted(self._tmp.iterdir())}",
        )


# ---------------------------------------------------------------------------
# Interactive disconnect
# ---------------------------------------------------------------------------


class InteractiveDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """`InteractiveSessionEnded` raised when the daemon dies
    mid-interactive-session."""

    async def asyncSetUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="p3-int-"))
        (self._tmp / ".actor").mkdir()

    async def asyncTearDown(self) -> None:
        _stop_daemon_in(self._tmp)
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_recv_raises_when_daemon_killed_mid_session(self) -> None:
        sock = self._tmp / ".actor" / "daemon.sock"
        fakes = Path(__file__).resolve().parent.parent / "e2e" / "fakes" / "bin"
        env = {
            **os.environ,
            "HOME": str(self._tmp),
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "FAKE_CLAUDE_INTERACTIVE": "1",
            "FAKE_CLAUDE_INTERACTIVE_QUIT": "NEVER",  # echo until SIGTERM
        }
        env.pop("ACTOR_NAME", None)
        proc = subprocess.Popen(
            ["actor", "daemon", "start", "--foreground"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if sock.exists():
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail("daemon failed to start")

            # Seed: actor + first run so a session id lands.
            actor_dir = self._tmp / "work"
            actor_dir.mkdir()
            svc = RemoteActorService(f"unix:{sock}", auto_spawn=False)
            try:
                await svc.new_actor(
                    name="alice", dir=str(actor_dir), no_worktree=True,
                    base=None, agent_name="claude", config=ActorConfig(),
                )
                await svc.run_actor(
                    name="alice", prompt="hi", config=ActorConfig(),
                )

                async with svc.interactive_session("alice") as session:
                    # Open the stream + read first stdout chunk so we
                    # know the daemon's PTY is up.
                    drained_one = False
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and not drained_one:
                        f = await asyncio.wait_for(session.recv(), timeout=2.0)
                        if f is not None and f.kind == "stdout":
                            drained_one = True

                    self.assertTrue(drained_one, "no stdout from PTY")

                    # Kill the daemon hard so it doesn't get a chance
                    # to send a clean ExitInfo.
                    proc.kill()
                    proc.wait(timeout=5)

                    # Next recv() should surface InteractiveSessionEnded.
                    with self.assertRaises(InteractiveSessionEnded):
                        for _ in range(20):
                            await asyncio.wait_for(session.recv(), timeout=1.0)
            finally:
                await svc.aclose()
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
