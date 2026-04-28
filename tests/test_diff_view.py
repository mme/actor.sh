"""Stage-1 logs-pattern parity for the watch DIFF tab.

Covers cancellation, token discard, hidden-tab stash + flush, and
width-aware cache invalidation. Mirrors the patterns the logs view
already uses; see `actor.watch.log_renderer` and the `_kick_log_build`
state machine in `actor.watch.app` for the reference implementation."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from actor.watch.app import ActorWatchApp
from actor.watch.diff_render import build_diff_renderables
from actor.watch.helpers import FileDiff


def _bare_app() -> ActorWatchApp:
    """Construct an ActorWatchApp without running its __init__ — the
    real init wires up Textual machinery (loop, DOM, DB) that we don't
    need for direct method tests. We attach only what each method
    reads, then drive the methods by hand."""
    app = ActorWatchApp.__new__(ActorWatchApp)
    # Reset class-level defaults to instance state so tests are
    # isolated from one another (class attrs are shared otherwise).
    app._diff_build_token = 0
    app._diff_build_pending = False
    app._diff_build_target_actor = None
    app._diff_build_target_width = 0
    app._diff_last_applied_key = None
    app._diff_pending_actor = None
    app._current_actors = []
    app._tab_base_labels = {
        "logs": "LIVE", "diff": "DIFF",
        "info": "OVERVIEW", "interactive": "INTERACTIVE",
    }
    return app


def _fake_actor(name: str = "alice") -> MagicMock:
    """Minimal stand-in for actor.types.Actor — only `.name` and
    `.dir` are read by the diff state machine."""
    a = MagicMock()
    a.name = name
    a.dir = f"/tmp/{name}"
    return a


def _scroll_with_width(width: int) -> MagicMock:
    """Stand-in for the VerticalScroll widget. The state machine
    only reads `.size.width` plus the mount/remove API."""
    scroll = MagicMock()
    scroll.size = MagicMock()
    scroll.size.width = width
    return scroll


# -- build_diff_renderables --------------------------------------------------


class BuildDiffRenderablesCancellationTests(unittest.TestCase):
    """The off-thread builder must cooperate with cancellation —
    return None as soon as `is_cancelled()` flips True. Mirrors the
    contract `build_log_renderables` already follows."""

    def test_cancels_before_starting(self):
        files = [FileDiff("a.txt", "old\n", "new\n")]
        result = build_diff_renderables(files, dark=True, is_cancelled=lambda: True)
        self.assertIsNone(result)

    def test_runs_to_completion_when_never_cancelled(self):
        files = [
            FileDiff("a.py", "x = 1\n", "x = 2\n"),
            FileDiff("b.py", "", "hello\n"),
        ]
        result = build_diff_renderables(files, dark=True)
        self.assertIsNotNone(result)
        parts, added, removed = result
        # Two files → at least 2 renderables, plus blank separators.
        self.assertGreaterEqual(len(parts), 2)
        # b.py is a "new file" with one added line; a.py replaces one
        # line so +1 / -1.
        self.assertEqual(added, 2)
        self.assertEqual(removed, 1)

    def test_cancels_mid_file_render(self):
        """`is_cancelled` flipping after the first file's render must
        abort before the second file is processed."""
        files = [
            FileDiff("first.py", "a\n", "b\n"),
            FileDiff("second.py", "c\n", "d\n"),
        ]
        # is_cancelled returns True on the 3rd call (after first file
        # rendered + post-render check). Counter chosen so the first
        # file completes but the second never starts.
        calls = {"n": 0}
        def is_cancelled() -> bool:
            calls["n"] += 1
            # Cancel after a few checks (between files).
            return calls["n"] > 3
        result = build_diff_renderables(files, dark=True, is_cancelled=is_cancelled)
        self.assertIsNone(result)


# -- App-level state machine -------------------------------------------------


class KickDiffBuildHiddenTabTests(unittest.TestCase):
    """When the DIFF pane is hidden TabbedContent collapses it to
    width 0. Kicking a build at width 0 caches segments at the wrong
    width; the kick path must skip and stash the actor for a later
    flush instead."""

    def test_kick_at_width_zero_stashes_actor_and_skips_build(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        with patch.object(app, "query_one", return_value=_scroll_with_width(0)), \
             patch.object(app, "set_timer") as set_timer, \
             patch.object(app, "_build_diff_worker") as worker:
            app._kick_diff_build(actor)
        self.assertEqual(app._diff_pending_actor, "alice")
        self.assertFalse(app._diff_build_pending)
        self.assertEqual(app._diff_build_token, 0)
        worker.assert_not_called()
        set_timer.assert_not_called()

    def test_kick_at_real_width_starts_worker_and_clears_stash(self):
        app = _bare_app()
        # Pre-stash from an earlier hidden-tab kick.
        app._diff_pending_actor = "alice"
        actor = _fake_actor("alice")
        with patch.object(app, "query_one", return_value=_scroll_with_width(100)), \
             patch.object(app, "set_timer"), \
             patch.object(app, "_build_diff_worker") as worker:
            app._kick_diff_build(actor)
        self.assertIsNone(app._diff_pending_actor)
        self.assertTrue(app._diff_build_pending)
        self.assertEqual(app._diff_build_token, 1)
        self.assertEqual(app._diff_build_target_actor, "alice")
        self.assertEqual(app._diff_build_target_width, 100)
        worker.assert_called_once()
        args, _kwargs = worker.call_args
        token, called_actor, called_width = args
        self.assertEqual(token, 1)
        self.assertIs(called_actor, actor)
        self.assertEqual(called_width, 100)

    def test_flush_pending_diff_kicks_when_pane_visible(self):
        """After a hidden-tab kick stashes an actor, the next tab
        activation should call `_flush_pending_diff_if_visible`,
        which re-kicks via `call_after_refresh`."""
        app = _bare_app()
        app._diff_pending_actor = "alice"
        actor = _fake_actor("alice")
        app._current_actors = [actor]

        # Capture the closure passed to call_after_refresh and run it
        # immediately so we can assert against the kick result.
        captured: dict[str, callable] = {}
        def fake_after_refresh(fn):
            captured["fn"] = fn

        with patch.object(app, "call_after_refresh", side_effect=fake_after_refresh):
            app._flush_pending_diff_if_visible()
        self.assertIn("fn", captured)

        with patch.object(app, "query_one", return_value=_scroll_with_width(80)), \
             patch.object(app, "set_timer"), \
             patch.object(app, "_build_diff_worker") as worker:
            captured["fn"]()
        # The stashed actor was found in _current_actors and re-kicked.
        worker.assert_called_once()

    def test_flush_noop_when_no_stash(self):
        app = _bare_app()
        with patch.object(app, "call_after_refresh") as after:
            app._flush_pending_diff_if_visible()
        after.assert_not_called()


class ApplyDiffBuildTokenDiscardTests(unittest.TestCase):
    """Stale builds (whose token has been superseded) must drop their
    output silently. Same discipline as the logs apply path."""

    def test_apply_diff_build_drops_when_token_stale(self):
        app = _bare_app()
        app._diff_build_token = 5  # main thread bumped past
        app._diff_build_pending = True
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll):
            app._apply_diff_build(
                token=3,  # stale
                cache_key=("alice", "deadbeef", 100),
                parts=[],
                total_added=10,
                total_removed=2,
            )
        # The stale apply touched neither the widget nor the cache.
        scroll.remove_children.assert_not_called()
        scroll.mount.assert_not_called()
        self.assertIsNone(app._diff_last_applied_key)
        # Pending flag is owned by the latest in-flight build; stale
        # apply must not clear it.
        self.assertTrue(app._diff_build_pending)

    def test_apply_diff_build_commits_when_token_matches(self):
        app = _bare_app()
        app._diff_build_token = 7
        app._diff_build_pending = True
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll), \
             patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_build(
                token=7,
                cache_key=("alice", "deadbeef", 100),
                parts=[],
                total_added=3,
                total_removed=4,
            )
        scroll.remove_children.assert_called_once()
        scroll.mount.assert_called_once()
        self.assertEqual(app._diff_last_applied_key, ("alice", "deadbeef", 100))
        self.assertFalse(app._diff_build_pending)
        # Tab label reflects the totals.
        self.assertIn("±7", app._tab_base_labels["diff"])

    def test_apply_diff_text_drops_when_token_stale(self):
        app = _bare_app()
        app._diff_build_token = 4
        app._diff_build_pending = True
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll):
            app._apply_diff_text(
                token=2,
                cache_key=("alice", "abc", 100),
                text="working tree clean",
            )
        scroll.mount.assert_not_called()
        self.assertIsNone(app._diff_last_applied_key)


class CacheKeyAndWidthChangeTests(unittest.TestCase):
    """`(actor.name, head_oid, content_width)` is the cache key. Same
    triple → no rebuild. Width change → rebuild even if everything
    else is unchanged."""

    def test_cache_hit_skips_compute_diff_and_clears_pending(self):
        app = _bare_app()
        app._diff_last_applied_key = ("alice", "deadbeef", 100)
        actor = _fake_actor("alice")
        # Simulate the kick that an actor-switch (or repeat-click)
        # would have made: bump token + set pending, then run worker.
        app._diff_build_token = 9
        app._diff_build_pending = True
        marks: list[int] = []
        with patch("actor.watch.app.read_head_oid", return_value="deadbeef"), \
             patch("actor.watch.app.compute_diff") as compute, \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
            with patch.object(app, "_mark_diff_build_done",
                              side_effect=marks.append):
                # Call the underlying function the @work decorator wraps.
                ActorWatchApp._build_diff_worker.__wrapped__(app, 9, actor, 100)
        # Cache hit — no compute_diff and the pending flag was cleared.
        compute.assert_not_called()
        self.assertEqual(marks, [9])

    def test_cache_miss_runs_compute_diff(self):
        app = _bare_app()
        # Different oid → cache miss.
        app._diff_last_applied_key = ("alice", "old-oid", 100)
        actor = _fake_actor("alice")
        app._diff_build_token = 11
        app._diff_build_pending = True
        # compute_diff returns "no files" → reason path so we don't
        # need real FileDiff objects to exercise the cache branch.
        result = MagicMock()
        result.files = None
        result.reason = "working tree clean"
        applied: list[tuple] = []
        with patch("actor.watch.app.read_head_oid", return_value="new-oid"), \
             patch("actor.watch.app.compute_diff", return_value=result) as compute, \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append((fn, a))):
            ActorWatchApp._build_diff_worker.__wrapped__(app, 11, actor, 100)
        compute.assert_called_once()
        self.assertEqual(len(applied), 1)
        fn, args = applied[0]
        # _apply_diff_text(token, cache_key, reason). Bound-method
        # objects compare by ==, not is — accessing the attribute
        # creates a fresh method instance each time.
        self.assertEqual(fn, app._apply_diff_text)
        self.assertEqual(args[0], 11)
        self.assertEqual(args[1], ("alice", "new-oid", 100))
        self.assertEqual(args[2], "working tree clean")

    def test_width_change_invalidates_cache(self):
        """Same actor + same HEAD but resized terminal → cache miss
        because content_width is part of the key."""
        app = _bare_app()
        app._diff_last_applied_key = ("alice", "deadbeef", 100)
        actor = _fake_actor("alice")
        app._diff_build_token = 12
        app._diff_build_pending = True
        result = MagicMock()
        result.files = None
        result.reason = ""
        with patch("actor.watch.app.read_head_oid", return_value="deadbeef"), \
             patch("actor.watch.app.compute_diff", return_value=result) as compute, \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: None):
            # Worker runs at width 140 — cache key differs by width.
            ActorWatchApp._build_diff_worker.__wrapped__(app, 12, actor, 140)
        compute.assert_called_once()


class WorkerCancellationTests(unittest.TestCase):
    """The worker observes cancellation via `_diff_build_token`
    changing on the main thread. A newer kick must short-circuit the
    in-flight worker before it commits."""

    def test_worker_bails_when_token_advances_before_compute(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        # Simulate a newer kick mid-worker by bumping the token before
        # the worker checks it.
        app._diff_build_token = 99
        app._diff_build_pending = True
        with patch("actor.watch.app.read_head_oid", return_value="x"), \
             patch("actor.watch.app.compute_diff") as compute, \
             patch.object(app, "call_from_thread") as cft:
            ActorWatchApp._build_diff_worker.__wrapped__(
                app, 1, actor, 100,  # stale token = 1, current = 99
            )
        compute.assert_not_called()
        cft.assert_not_called()


if __name__ == "__main__":
    unittest.main()
