"""Pilot regression for issue #93 — `actor watch`'s "Loading logs..."
placeholder must not flash over already-rendered logs.

The placeholder is meant to cover the cold-start case (initial paint of
an actor whose logs haven't arrived yet). When a same-actor rebuild
fires (terminal resize, a poll whose tail contains a TOOL_RESULT, etc.)
the previous render must stay on screen until `_apply_log_build` swaps
in the new content; wiping it for "Loading logs..." mid-stream is the
visible regression."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


def _bucket_text(log) -> str:
    return "\n".join(strip.text for strip in log.lines)


class WatchLogPlaceholderTests(unittest.IsolatedAsyncioTestCase):
    def _setup_home(self) -> str:
        tmpdir = tempfile.mkdtemp(prefix="watch-log-placeholder-")
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

    async def _wait_until(self, predicate, pilot, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await pilot.pause(0.05)
        raise AssertionError(
            f"timed out after {timeout}s waiting for condition"
        )

    async def test_placeholder_does_not_flash_on_same_actor_rebuild(self):
        """First paint commits with content, then a same-actor rebuild is
        forced (TOOL_RESULT tail). The 300ms placeholder timer fires
        because we slow `build_log_renderables` past it — but with the
        fix in place the placeholder must NOT replace the rendered logs.
        """
        from actor.interfaces import LogEntry, LogEntryKind
        from textual.widgets import RichLog

        app = await self._boot_app()
        pre_existing = [
            LogEntry(kind=LogEntryKind.USER, text="hello world"),
            LogEntry(kind=LogEntryKind.ASSISTANT, text="hi there"),
            LogEntry(kind=LogEntryKind.USER, text="another prompt"),
        ]

        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash(pilot, app)

            # Pre-seed the per-actor bucket so selecting alpha paints
            # real content (instead of the empty "No logs yet" path).
            app._log_entries_by_actor["alpha"] = list(pre_existing)

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

            log = app.query_one("#logs-content", RichLog)

            # Wait until first paint commits — _last_log_count tracks
            # what's actually been rendered to the widget.
            await self._wait_until(
                lambda: app._last_log_actor == "alpha"
                and app._last_log_count == len(pre_existing),
                pilot,
                timeout=3.0,
            )
            self.assertGreater(
                len(log.lines), 0,
                "precondition: log must have content for alpha after first paint",
            )
            self.assertNotIn("Loading logs...", _bucket_text(log))

            # Now force a same-actor rebuild and slow the build past the
            # 300ms placeholder timer. Appending a TOOL_RESULT entry
            # makes can_append=False so _set_logs falls through to the
            # full-rebuild path.
            from actor.watch import app as watch_app

            real_build = watch_app.build_log_renderables

            def slow_build(entries, colors, is_cancelled=None):
                # Sleep long enough that the 300ms placeholder timer
                # fires before the worker hands off to _apply_log_build.
                time.sleep(0.5)
                if is_cancelled is None:
                    return real_build(entries, colors)
                return real_build(entries, colors, is_cancelled)

            with patch.object(watch_app, "build_log_renderables", slow_build):
                bucket = app._log_entries_by_actor["alpha"]
                bucket.append(LogEntry(
                    kind=LogEntryKind.TOOL_RESULT, content="result text",
                ))
                app._set_logs("alpha", bucket)

                # Poll across the 300ms timer window — at no point
                # should the widget show the placeholder.
                deadline = time.monotonic() + 0.45
                while time.monotonic() < deadline:
                    self.assertNotIn(
                        "Loading logs...", _bucket_text(log),
                        "placeholder flashed during a same-actor rebuild "
                        "(regression of #93)",
                    )
                    await pilot.pause(0.03)

            # Wait for the new build to commit, then sanity-check that
            # the rebuild actually completed (i.e. we did exercise the
            # full-rebuild path, not just the no-op short-circuit).
            await self._wait_until(
                lambda: app._last_log_count == len(bucket),
                pilot,
                timeout=3.0,
            )
            self.assertNotIn("Loading logs...", _bucket_text(log))

    async def test_placeholder_does_appear_when_no_content_yet(self):
        """Companion guard: the placeholder is still useful for the
        cold-start case. A slow first paint with no prior content for
        any actor must still flash 'Loading logs...' via the 300ms
        timer — otherwise the user sees a blank pane forever."""
        from actor.interfaces import LogEntry, LogEntryKind
        from textual.widgets import RichLog

        app = await self._boot_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash(pilot, app)

            entries = [
                LogEntry(kind=LogEntryKind.USER, text="hello"),
                LogEntry(kind=LogEntryKind.ASSISTANT, text="hi"),
            ]
            app._log_entries_by_actor["alpha"] = entries

            from actor.watch import app as watch_app
            real_build = watch_app.build_log_renderables

            def slow_build(es, colors, is_cancelled=None):
                time.sleep(0.5)
                if is_cancelled is None:
                    return real_build(es, colors)
                return real_build(es, colors, is_cancelled)

            log = app.query_one("#logs-content", RichLog)

            with patch.object(watch_app, "build_log_renderables", slow_build):
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

                # First-paint kick is slow → placeholder timer fires
                # ~300ms in and replaces the empty widget with the
                # placeholder. We poll for it.
                saw_placeholder = False
                deadline = time.monotonic() + 0.45
                while time.monotonic() < deadline:
                    if "Loading logs..." in _bucket_text(log):
                        saw_placeholder = True
                        break
                    await pilot.pause(0.03)
                self.assertTrue(
                    saw_placeholder,
                    "placeholder should have appeared during a slow first "
                    "paint with no prior content for the actor",
                )


if __name__ == "__main__":
    unittest.main()
