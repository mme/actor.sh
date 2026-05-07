"""Soak test for actord (Phase 3.5).

Drives the daemon under sustained simulated load and asserts memory /
FD / log-rotation behaviour. Skipped by default — explicitly opt in
with `ACTOR_RUN_SOAK=1` because each run takes 30+ minutes.

Workload per cycle:
- Spawn N actors (`SOAK_ACTOR_COUNT`, default 5) via `new_actor`,
  kick a `run_actor` on each (fake claude, sleep=0.2s), list/show
  in flight, then `discard_actor` to free the slot. Actor names are
  cycled so the next batch reuses them after discard.
- A separate `RemoteActorService` subscribes to notifications and
  counts `run_completed` events. Every ~30 cycles it simulates a
  network blip (`aclose()` + open a fresh service) to exercise
  Phase 3's auto-reconnect.
- Every minute: snapshot daemon RSS / FD count / GetServerInfo
  connection count / daemon.log size / actor.db size. Append to
  the metrics CSV.

Final assertions (DoD):
- Daemon process is still alive (no crash).
- RSS hasn't grown by more than 50MB beyond the warmup baseline.
- FD count is within ±10% of the post-warmup baseline (no leak).
- The number of `run_completed` events seen by the subscriber
  matches the number of completed `run_actor` calls (modulo the
  brief gaps during simulated network blips, which are explicitly
  budgeted).
- daemon.log.1 exists if daemon.log ever crossed 10MB.
"""
from __future__ import annotations

import asyncio
import csv
import os
import shutil
import signal
import socket as _socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional, Tuple

from actor.bootstrap import is_pid_alive, read_daemon_pid
from actor.service import Notification, RemoteActorService
from actor.types import ActorConfig, Status


_FAKES_BIN = (
    Path(__file__).resolve().parent.parent / "e2e" / "fakes" / "bin"
).resolve()


def _env(seconds: float | None = None) -> int:
    raw = os.environ.get("SOAK_DURATION")
    if raw is None:
        return int(seconds) if seconds is not None else 1800
    return int(raw)


def _actor_count() -> int:
    return int(os.environ.get("SOAK_ACTOR_COUNT", "5"))


def _metrics_path() -> Path:
    return Path(os.environ.get("SOAK_METRICS_PATH", "/tmp/actord-soak-metrics.csv"))


def _socket_accepts(socket_path: str) -> bool:
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        sock.connect(socket_path)
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _read_rss_kb(pid: int) -> Optional[int]:
    """Resident-set size in KB from `/proc/<pid>/status`. Returns
    None if the process is gone."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


def _open_fd_count(pid: int) -> Optional[int]:
    try:
        return len(list(Path(f"/proc/{pid}/fd").iterdir()))
    except OSError:
        return None


@unittest.skipUnless(
    os.environ.get("ACTOR_RUN_SOAK"),
    "Soak test skipped by default; set ACTOR_RUN_SOAK=1 to enable",
)
class DaemonSoakTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="actord-soak-"))
        (self._tmp / ".actor").mkdir()
        self._daemon_proc: Optional[subprocess.Popen] = None
        self._sock = self._tmp / ".actor" / "daemon.sock"
        # Sequence numbers for actor names so we cycle through
        # without collisions after discard.
        self._next_actor_id = 0

    async def asyncTearDown(self) -> None:
        proc = self._daemon_proc
        if proc is not None and proc.poll() is None:
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        # Belt-and-braces: nothing left running for this HOME.
        pid = read_daemon_pid(self._tmp / ".actor" / "daemon.pid")
        if pid is not None and is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _start_daemon(self) -> int:
        """Boot the daemon under the test HOME. Returns the PID."""
        env = {
            **os.environ,
            "HOME": str(self._tmp),
            "PATH": f"{_FAKES_BIN}:{os.environ.get('PATH', '')}",
            # Soak runs use a fast fake claude — 0.2s sleep so each
            # run takes ~half a second total. Realistic enough to
            # exercise the run pipeline without making the soak
            # dominated by agent latency.
            "FAKE_CLAUDE_RESPONSE": "ok",
            "FAKE_CLAUDE_SLEEP": "0.2",
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
        self._daemon_proc = proc
        for _ in range(200):
            if proc.poll() is not None:
                raise RuntimeError(
                    f"daemon exited early (rc={proc.returncode}) during soak boot",
                )
            if _socket_accepts(str(self._sock)):
                return proc.pid
            await asyncio.sleep(0.05)
        raise RuntimeError("daemon failed to start within 10s")

    async def test_daemon_stays_healthy_under_load(self) -> None:
        duration = _env()
        actor_count = _actor_count()
        metrics_path = _metrics_path()
        print(
            f"\n[soak] duration={duration}s actor_count={actor_count} "
            f"metrics={metrics_path}",
            flush=True,
        )

        pid = await self._start_daemon()
        print(f"[soak] daemon up, pid={pid}", flush=True)

        # Two services: one for the workload, one for the
        # subscriber. Subscriber gets bounced periodically to test
        # auto-reconnect.
        worker = RemoteActorService(f"unix:{self._sock}", auto_spawn=False)
        subscriber = RemoteActorService(f"unix:{self._sock}", auto_spawn=False)

        events_seen = 0

        async def _on_notification(n: Notification) -> None:
            nonlocal events_seen
            if n.event == "run_completed":
                events_seen += 1

        # Open subscription. We hold it as a (handler, cancel) pair
        # so we can blip it.
        sub_state = {"cancel": None}
        sub_state["cancel"] = await subscriber.subscribe_notifications(_on_notification)

        # CSV header + warmup row.
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with metrics_path.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "elapsed_s", "rss_kb", "open_fds", "connections",
                "log_size", "db_size", "events_seen", "events_published",
            ])

        events_published = 0
        cycle = 0
        # Run a brief warmup before sampling baselines so RSS / FD
        # counts settle past first-use peaks (channel open, codec
        # tables loaded, etc.).
        WARMUP_SECONDS = 30.0
        warmup_baseline: Optional[Tuple[int, int]] = None  # (rss_kb, fds)

        async def _run_cycle() -> int:
            """One cycle: spawn `actor_count` actors, run them,
            list/show, discard. Returns count of run_completed
            events that should fire (one per run)."""
            nonlocal events_published
            names: list[str] = []
            for _ in range(actor_count):
                name = f"act{self._next_actor_id}"
                self._next_actor_id += 1
                names.append(name)
                await worker.new_actor(
                    name=name, dir=str(self._tmp), no_worktree=True,
                    base=None, agent_name="claude", config=ActorConfig(),
                )

            # Kick off runs concurrently; await all.
            run_tasks = [
                asyncio.create_task(
                    worker.run_actor(name=n, prompt="x", config=ActorConfig()),
                )
                for n in names
            ]
            # Mid-flight: list + show.
            actors = await worker.list_actors()
            assert len(actors) >= len(names)
            await worker.show_actor(names[0])

            # Wait for all runs to finish.
            for t in run_tasks:
                result = await t
                events_published += 1
                assert result.status in (Status.DONE, Status.STOPPED), (
                    f"unexpected run status: {result.status}"
                )

            # Discard so the next cycle starts fresh.
            for n in names:
                await worker.discard_actor(n, force=True)
            return len(names)

        async def _sample_metrics(elapsed: float) -> None:
            nonlocal warmup_baseline
            rss = _read_rss_kb(pid)
            fds = _open_fd_count(pid)
            try:
                info = await worker.get_server_info()
                conns = info.connection_count
            except Exception:
                conns = -1
            log = self._tmp / ".actor" / "daemon.log"
            db = self._tmp / ".actor" / "actor.db"
            with metrics_path.open("a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    f"{elapsed:.1f}",
                    rss if rss is not None else -1,
                    fds if fds is not None else -1,
                    conns,
                    log.stat().st_size if log.exists() else 0,
                    db.stat().st_size if db.exists() else 0,
                    events_seen,
                    events_published,
                ])
            if warmup_baseline is None and elapsed >= WARMUP_SECONDS:
                if rss is not None and fds is not None:
                    warmup_baseline = (rss, fds)
                    print(
                        f"[soak] warmup baseline: rss={rss}KB fds={fds}",
                        flush=True,
                    )

        try:
            start = time.monotonic()
            next_metric = start + 60.0
            log_rotation_seen = False
            while True:
                elapsed = time.monotonic() - start
                if elapsed >= duration:
                    break
                # Workload tick.
                await _run_cycle()
                cycle += 1

                # Periodic blip: every 30 cycles, drop and re-open
                # the subscriber to exercise auto-reconnect. Skip
                # for the first 5 cycles so warmup metrics are
                # sane.
                if cycle >= 5 and cycle % 30 == 0:
                    cancel = sub_state["cancel"]
                    if cancel is not None:
                        cancel()
                        await asyncio.sleep(0.1)
                    await subscriber.aclose()
                    subscriber = RemoteActorService(
                        f"unix:{self._sock}", auto_spawn=False,
                    )
                    sub_state["cancel"] = await subscriber.subscribe_notifications(
                        _on_notification,
                    )
                    print(
                        f"[soak] cycle={cycle} elapsed={elapsed:.0f}s "
                        f"events={events_seen}/{events_published} blip",
                        flush=True,
                    )

                # Metrics snapshot every minute.
                if time.monotonic() >= next_metric:
                    await _sample_metrics(elapsed)
                    next_metric = time.monotonic() + 60.0
                    log = self._tmp / ".actor" / "daemon.log.1"
                    if log.exists():
                        log_rotation_seen = True

            # Final sample.
            await _sample_metrics(time.monotonic() - start)
        finally:
            # Drain any in-flight notifications before tearing the
            # subscriber down — gives the auto-reconnect loop a
            # last tick.
            await asyncio.sleep(0.5)
            cancel = sub_state["cancel"]
            if cancel is not None:
                cancel()
                await asyncio.sleep(0.1)
            await subscriber.aclose()
            await worker.aclose()

        # ---- Assertions ----------------------------------------------------

        # Daemon alive.
        self.assertTrue(
            is_pid_alive(pid),
            f"daemon (pid {pid}) died during soak",
        )

        # RSS within budget (50MB above warmup baseline).
        final_rss = _read_rss_kb(pid)
        self.assertIsNotNone(final_rss, "could not read final RSS")
        if warmup_baseline is not None:
            base_rss = warmup_baseline[0]
            growth_kb = final_rss - base_rss
            print(
                f"[soak] RSS: warmup={base_rss}KB final={final_rss}KB "
                f"growth={growth_kb}KB",
                flush=True,
            )
            self.assertLessEqual(
                growth_kb, 50 * 1024,
                f"RSS grew by {growth_kb}KB (> 50MB) during soak",
            )

        # FD count stable (±10% of warmup baseline).
        final_fds = _open_fd_count(pid)
        self.assertIsNotNone(final_fds, "could not read final FD count")
        if warmup_baseline is not None:
            base_fds = warmup_baseline[1]
            allowed = max(int(base_fds * 0.10), 5)
            print(
                f"[soak] FDs: warmup={base_fds} final={final_fds} "
                f"allowed_drift=±{allowed}",
                flush=True,
            )
            self.assertLessEqual(
                abs(final_fds - base_fds), allowed,
                f"FD count drifted by {final_fds - base_fds} "
                f"(allowed ±{allowed}) — possible leak",
            )

        # Notification delivery: every published event reached the
        # subscriber, modulo the gap during blips. Each blip can lose
        # at most a couple of events that landed between cancel + open;
        # budget = 1 per blip + 5 slack.
        blip_count = max(0, (cycle - 5) // 30)
        budget = blip_count + 5
        delta = events_published - events_seen
        print(
            f"[soak] notifications: published={events_published} "
            f"seen={events_seen} delta={delta} budget={budget}",
            flush=True,
        )
        self.assertLessEqual(
            delta, budget,
            f"lost {delta} notifications (budget {budget})",
        )

        # Log rotation: only required if the log actually crossed
        # the 10MB cap.
        log = self._tmp / ".actor" / "daemon.log"
        if log.exists() and log.stat().st_size > 10 * 1024 * 1024:
            self.assertTrue(
                log_rotation_seen or (self._tmp / ".actor" / "daemon.log.1").exists(),
                "daemon.log crossed 10MB but no daemon.log.1 was created",
            )
        print(
            f"[soak] PASS — cycles={cycle} runs={events_published} "
            f"metrics CSV at {metrics_path}",
            flush=True,
        )


if __name__ == "__main__":
    unittest.main()
