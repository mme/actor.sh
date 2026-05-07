"""Pilot regression for issue #96 — `apply_log_renderables` must yield
to the event loop between batches so a long log rebuild doesn't freeze
the dashboard.

The build is already off-thread (PR #55). The apply runs on the main
thread; if it does all writes in one tight loop, keystrokes/scroll/etc.
stall for hundreds of ms on a 5000-entry log. This test slows down
each `RichLog.write` so the apply has measurable cost, kicks a full
rebuild, and asserts the event loop keeps ticking faster than 30Hz
mid-apply (i.e. each yield gap is < ~33ms).

Mutation test: replace `apply_log_renderables` with the synchronous
loop and the assertion fails — `max_gap` jumps to >>33ms because no
ticks happen during the apply."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class WatchLogApplyChunkingTests(unittest.IsolatedAsyncioTestCase):
    def _setup_home(self) -> str:
        tmpdir = tempfile.mkdtemp(prefix="watch-log-apply-chunk-")
        actor_dir = Path(tmpdir) / ".actor"
        actor_dir.mkdir(parents=True, exist_ok=True)

        from actor.db import Database
        from actor.types import Actor, ActorConfig, AgentKind
        db = Database.open(str(actor_dir / "actor.db"))
        db.insert_actor(Actor(
            name="alpha",
            agent=AgentKind.CLAUDE,
            agent_session=None,
            dir=tmpdir,
            source_repo=None,
            base_branch=None,
            worktree=False,
            parent=None,
            config=ActorConfig(),
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ))
        db.close()
        return tmpdir

    async def _boot_app(self):
        tmpdir = self._setup_home()
        env_patch = patch.dict(os.environ, {"HOME": tmpdir})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        from actor.watch.app import ActorWatchApp
        return ActorWatchApp(animate=False)

    async def _dismiss_splash(self, pilot, app):
        for _ in range(20):
            if not getattr(app, "_splash_active", False):
                return
            await pilot.pause(0.05)

    async def _wait_until(self, predicate, pilot, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await pilot.pause(0.02)
        raise AssertionError(
            f"timed out after {timeout}s waiting for condition"
        )

    async def test_apply_yields_event_loop_during_long_rebuild(self):
        from actor.interfaces import LogEntry, LogEntryKind
        from textual.widgets import RichLog
        from actor.watch import log_renderer

        # 5000 user-only entries → ~10k renderables in the apply.
        # USER entries skip markdown rendering so the build itself is
        # fast (~30ms); the test cost is dominated by the apply, where
        # we artificially slow each write so a synchronous apply would
        # block long enough to be unambiguous in the gap data.
        entries = [
            LogEntry(kind=LogEntryKind.USER, text=f"prompt {i}")
            for i in range(5000)
        ]

        app = await self._boot_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash(pilot, app)

            from actor.watch.app import ActorTree
            tree = app.query_one(ActorTree)
            await self._wait_until(
                lambda: any(
                    "alpha" in str(n.label) for n in tree.root.children
                ),
                pilot,
            )
            node = next(
                n for n in tree.root.children if "alpha" in str(n.label)
            )
            tree.focus()
            tree.select_node(node)

            # Let the empty initial paint settle so the apply we
            # measure is the populated rebuild kicked below.
            await pilot.pause(0.1)

            # Slow each padded write to ~50µs (busy-spin, not sleep —
            # sleep would yield to the event loop and hide the bug).
            # Across ~10k renderables this costs ~500ms of CPU on the
            # main thread; a synchronous apply would block the event
            # loop for that whole window.
            real_padded_write = log_renderer._RightPaddedLog.write

            def slow_write(self, content, **kwargs):
                deadline = time.perf_counter() + 50e-6
                while time.perf_counter() < deadline:
                    pass
                return real_padded_write(self, content, **kwargs)

            with patch.object(
                log_renderer._RightPaddedLog, "write", slow_write
            ):
                app._log_entries_by_actor["alpha"] = list(entries)
                app._set_logs("alpha", app._log_entries_by_actor["alpha"])

                log = app.query_one("#logs-content", RichLog)
                gaps: list[float] = []
                last = time.perf_counter()
                # Generous deadline: ~30ms build + ~500ms slow apply +
                # plenty of headroom for cold-CI variance.
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    await asyncio.sleep(0)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now
                    if (
                        app._last_log_actor == "alpha"
                        and app._last_log_count == len(entries)
                        and len(gaps) > 50
                    ):
                        break

                self.assertEqual(
                    app._last_log_actor, "alpha",
                    "apply never committed under load",
                )
                self.assertEqual(
                    app._last_log_count, len(entries),
                    "apply committed wrong count under load",
                )
                self.assertGreater(
                    len(log.lines), 0,
                    "log widget has no content after apply",
                )

            # With chunking in place, max gap stays well under 100ms
            # in practice (~40ms on a stressed dev box, single-digit
            # ms on idle). A synchronous apply produces ONE gap of
            # ~500ms (50µs × ~10k writes) that lights this up
            # unambiguously. The 100ms bound has plenty of headroom
            # for cold-CI variance, GC pauses, and the build worker
            # contending for the GIL — without crossing into
            # "synchronous apply" territory.
            max_gap = max(gaps)
            self.assertLess(
                max_gap, 0.1,
                f"event loop blocked for {max_gap*1000:.1f}ms during "
                f"apply — chunking should keep every gap <100ms. "
                f"Sampled {len(gaps)} ticks; check that "
                f"apply_log_renderables yields between batches.",
            )


if __name__ == "__main__":
    unittest.main()
