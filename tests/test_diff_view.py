"""Stage-1 + Stage-2 tests for the watch DIFF tab.

Stage 1 — logs-pattern parity: cancellation, token discard, hidden-tab
stash + flush, width-aware cache invalidation. Mirrors the patterns
the logs view already uses; see `actor.watch.log_renderer` and the
`_kick_log_build` state machine in `actor.watch.app`.

Stage 2 — batched git invocations: `compute_diff` collapses N+5
subprocess spawns into a constant ≤4 regardless of file count, and
parsed ± counts ride through `FileDiff.added` / `removed` so the
renderer doesn't re-run difflib just to label the tab."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from rich.segment import Segment
from rich.text import Text
from textual.strip import Strip

from actor.watch.app import ActorWatchApp
from actor.watch.diff_render import build_diff_renderables
from actor.watch.prerendered_diff import PrerenderedDiff
from actor.watch.helpers import (
    FileDiff,
    _git_cat_file_batch,
    _parse_diff_files,
    compute_diff,
    compute_diff_shortstat,
    git_index_mtime,
    parse_shortstat,
)
from actor.types import Status


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
    app._diff_badge_token = 0
    app._diff_badge_target_actor = None
    app._diff_poll_initialized = False
    app._diff_poll_last_actor = None
    app._diff_poll_last_index_mtime = None
    app._diff_poll_last_shortstat = None
    app._diff_poll_last_untracked = None
    app._prev_statuses = {}
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


@contextmanager
def _stub_theme(dark: bool = True):
    """Patch `ActorWatchApp.current_theme` so worker tests can read
    `is_dark` without paying for the full Textual app setup. The
    reactive descriptor on the App class needs PropertyMock to override
    cleanly — direct attribute assignment goes through Reactive.__set__
    which complains about the missing app context."""
    theme = MagicMock()
    theme.dark = dark
    with patch.object(
        ActorWatchApp, "current_theme",
        new_callable=PropertyMock, return_value=theme,
    ):
        yield


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
        # Stage-5 fix: kick passes `force` through to the worker so
        # live-poll-driven force-refreshes bypass the cache-key
        # short-circuit. Default kick is force=False.
        token, called_actor, called_width, force = args
        self.assertEqual(token, 1)
        self.assertIs(called_actor, actor)
        self.assertEqual(called_width, 100)
        self.assertFalse(force)

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
    output silently. Same discipline as the logs apply path. Stage 4
    splits the old single-call apply into per-file streaming
    (`_diff_append_file`) plus a finalizer (`_apply_diff_build_done`);
    stale-token discipline applies to both."""

    def test_diff_append_file_drops_when_token_stale(self):
        app = _bare_app()
        app._diff_build_token = 5  # main thread bumped past
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll):
            app._diff_append_file(
                token=3,  # stale
                file_path="x.py",
                strips=[Strip([Segment("hi")])],
            )
        scroll.remove_children.assert_not_called()
        scroll.mount.assert_not_called()
        # Streamed-token sentinel stays put; the next live append for
        # the current token will still trigger a clear.
        self.assertEqual(app._diff_streamed_token, -1)

    def test_apply_diff_build_done_drops_when_token_stale(self):
        app = _bare_app()
        app._diff_build_token = 5
        app._diff_build_pending = True
        with patch.object(app, "_update_diff_tab_label") as upd:
            app._apply_diff_build_done(
                token=3,  # stale
                cache_key=("alice", "deadbeef", 100),
                total_added=10,
                total_removed=2,
            )
        upd.assert_not_called()
        self.assertIsNone(app._diff_last_applied_key)
        # Pending flag is owned by the latest in-flight build; stale
        # finalizer must not clear it.
        self.assertTrue(app._diff_build_pending)

    def test_apply_diff_build_done_commits_when_token_matches(self):
        app = _bare_app()
        app._diff_build_token = 7
        app._diff_build_pending = True
        with patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_build_done(
                token=7,
                cache_key=("alice", "deadbeef", 100),
                total_added=3,
                total_removed=4,
            )
        self.assertEqual(app._diff_last_applied_key, ("alice", "deadbeef", 100))
        self.assertFalse(app._diff_build_pending)
        # New label shape: "DIFF +3 -4" (added 3, removed 4); colors live
        # in segments. Assert on the plain text.
        self.assertEqual(app._tab_base_labels["diff"].plain, "DIFF +3 -4")

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


# -- Stage 2: batched git invocations ---------------------------------------


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    """Run a git subprocess in `cwd`, raise on failure. Configured to
    skip the user's commit signing / hooks so the temp repo doesn't
    inherit machine-specific git config."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd, env=env, check=True,
        capture_output=True, text=True,
    )


def _build_repo(n_files: int) -> str:
    """Create a temporary git repo with `n_files` files committed and
    then modified in the working tree. Returns the worktree path. The
    caller is responsible for cleanup."""
    tmp = tempfile.mkdtemp(prefix="actor-diff-stress-")
    _git("init", "-q", "-b", "main", tmp, cwd=tmp)
    for i in range(n_files):
        Path(tmp, f"file_{i:03d}.txt").write_text(f"old content {i}\nline 2\n")
    _git("add", "-A", cwd=tmp)
    _git("commit", "-q", "-m", "initial", cwd=tmp)
    # Modify every file so all show up in the merge-base diff.
    for i in range(n_files):
        Path(tmp, f"file_{i:03d}.txt").write_text(f"new content {i}\nline 2\n")
    return tmp


def _fake_actor_for_repo(tmp: str) -> MagicMock:
    a = MagicMock()
    a.dir = tmp
    a.base_branch = "main"
    a.name = "stress"
    return a


class _SubprocessCounter:
    """Wraps the real `subprocess` module and records every spawn the
    helpers module triggers via `subprocess.run` / `subprocess.Popen`.

    Patching the module-attribute (e.g. ``subprocess.run``) directly
    has a nasty interaction: ``subprocess.run`` internally calls
    ``Popen`` through the same module object, so a patched run + a
    patched Popen would each record one extra call per `run`. This
    wrapper sidesteps that by replacing the entire `subprocess`
    reference inside `actor.watch.helpers` — only that module's
    explicit calls get recorded; the implementation-detail Popen
    calls inside the real `subprocess.run` are unaffected."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.PIPE = subprocess.PIPE

    def run(self, args, *a, **kw):
        self.calls.append(list(args))
        return subprocess.run(args, *a, **kw)

    def Popen(self, args, *a, **kw):
        self.calls.append(list(args))
        return subprocess.Popen(args, *a, **kw)


class ComputeDiffSubprocessCountTests(unittest.TestCase):
    """The whole point of Stage 2: subprocess count is bounded
    regardless of how many files changed. Each test below spawns a
    real temp git repo so the parser + cat-file batch are exercised
    end-to-end against actual git output, not a hand-rolled fixture."""

    def test_50_file_diff_uses_bounded_subprocess_count(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = _build_repo(n_files=50)
        try:
            actor = _fake_actor_for_repo(tmp)
            counter = _SubprocessCounter()
            with patch("actor.watch.helpers.subprocess", counter):
                result = compute_diff(actor)

            # Acceptance bar from the plan: ~3 git calls plus 1
            # ls-files. Stage 2 hits exactly four for a tracked-only
            # diff:
            #   1. git merge-base
            #   2. git diff --no-color <base>
            #   3. git ls-files --others --exclude-standard
            #   4. git cat-file --batch
            self.assertLessEqual(
                len(counter.calls), 4,
                f"compute_diff spawned {len(counter.calls)} "
                f"subprocesses for 50 files; commands were "
                f"{counter.calls!r}",
            )
            # cat-file was the one that used to scale with N — make
            # sure it's now a single call.
            cat_file_calls = [
                c for c in counter.calls if c[:2] == ["git", "cat-file"]
            ]
            self.assertEqual(
                len(cat_file_calls), 1,
                f"expected a single cat-file batch; got {cat_file_calls!r}",
            )
            # Result is correct — every modified file shows up with
            # ± counts pulled straight from git diff (no extra
            # difflib pass).
            self.assertIsNotNone(result.files)
            self.assertEqual(len(result.files), 50)
            for fd in result.files:
                self.assertEqual(fd.added, 1)
                self.assertEqual(fd.removed, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pure_untracked_skips_cat_file_batch(self):
        """No tracked changes → no old-content reads needed → no
        cat-file invocation. Bounded above by 3 subprocesses."""
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = tempfile.mkdtemp(prefix="actor-diff-untracked-")
        try:
            _git("init", "-q", "-b", "main", tmp, cwd=tmp)
            Path(tmp, "seed.txt").write_text("seed\n")
            _git("add", "-A", cwd=tmp)
            _git("commit", "-q", "-m", "seed", cwd=tmp)
            for i in range(10):
                Path(tmp, f"new_{i}.txt").write_text(f"hi {i}\n")
            actor = _fake_actor_for_repo(tmp)

            counter = _SubprocessCounter()
            with patch("actor.watch.helpers.subprocess", counter):
                result = compute_diff(actor)

            cat_file_calls = [
                c for c in counter.calls if c[:2] == ["git", "cat-file"]
            ]
            self.assertEqual(
                cat_file_calls, [],
                "no tracked changes → cat-file --batch shouldn't run",
            )
            self.assertLessEqual(len(counter.calls), 3)
            self.assertIsNotNone(result.files)
            self.assertEqual(len(result.files), 10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ParseDiffFilesTests(unittest.TestCase):
    """Per-file metadata extraction from `git diff --no-color` output.
    Drives the ± counts that ride through FileDiff into the renderer."""

    def test_modified_file_counts(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index abc..def 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " ctx\n"
            "-removed\n"
            "+added one\n"
            "+added two\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "foo.py")
        self.assertEqual(files[0]["added"], 2)
        self.assertEqual(files[0]["removed"], 1)
        self.assertFalse(files[0]["is_new"])
        self.assertFalse(files[0]["is_deleted"])
        self.assertFalse(files[0]["is_binary"])

    def test_new_file(self):
        diff = (
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "index 0000000..abc\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line one\n"
            "+line two\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["is_new"])
        self.assertEqual(files[0]["added"], 2)
        self.assertEqual(files[0]["removed"], 0)
        self.assertEqual(files[0]["path"], "new.py")

    def test_deleted_file(self):
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "index abc..0000000\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-bye\n"
            "-bye again\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["is_deleted"])
        self.assertEqual(files[0]["removed"], 2)
        # The path lives on `--- a/...` for deletes — used to look up
        # the merge-base content via cat-file.
        self.assertEqual(files[0]["path"], "gone.py")

    def test_binary_file_marked_no_counts(self):
        diff = (
            "diff --git a/img.png b/img.png\n"
            "index abc..def 100644\n"
            "Binary files a/img.png and b/img.png differ\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["is_binary"])
        self.assertEqual(files[0]["added"], 0)
        self.assertEqual(files[0]["removed"], 0)

    def test_multiple_hunks_aggregate(self):
        diff = (
            "diff --git a/multi.py b/multi.py\n"
            "--- a/multi.py\n"
            "+++ b/multi.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n"
            "+a2\n"
            "@@ -10,2 +10,3 @@\n"
            " ctx\n"
            "+b\n"
            "+c\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(files[0]["added"], 3)
        self.assertEqual(files[0]["removed"], 1)

    def test_no_newline_marker_ignored(self):
        diff = (
            "diff --git a/x.txt b/x.txt\n"
            "--- a/x.txt\n"
            "+++ b/x.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(files[0]["added"], 1)
        self.assertEqual(files[0]["removed"], 1)


class GitCatFileBatchTests(unittest.TestCase):
    """One cat-file --batch call satisfies all old-content reads.
    Round-trip with a real git invocation to keep the protocol-parsing
    honest — easy to break in subtle ways without an integration test."""

    def test_batch_returns_contents_for_existing_refs(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = tempfile.mkdtemp(prefix="actor-cat-file-")
        try:
            _git("init", "-q", "-b", "main", tmp, cwd=tmp)
            Path(tmp, "a.txt").write_text("alpha\n")
            Path(tmp, "b.txt").write_text("bravo\nbravo2\n")
            _git("add", "-A", cwd=tmp)
            _git("commit", "-q", "-m", "seed", cwd=tmp)
            head = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

            contents = _git_cat_file_batch(
                [f"{head}:a.txt", f"{head}:b.txt"], tmp,
            )
            self.assertEqual(contents[f"{head}:a.txt"], "alpha\n")
            self.assertEqual(contents[f"{head}:b.txt"], "bravo\nbravo2\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_ref_returns_empty_string(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = tempfile.mkdtemp(prefix="actor-cat-file-missing-")
        try:
            _git("init", "-q", "-b", "main", tmp, cwd=tmp)
            Path(tmp, "real.txt").write_text("real\n")
            _git("add", "-A", cwd=tmp)
            _git("commit", "-q", "-m", "seed", cwd=tmp)
            head = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip()

            contents = _git_cat_file_batch(
                [f"{head}:real.txt", f"{head}:does-not-exist.txt"], tmp,
            )
            self.assertEqual(contents[f"{head}:real.txt"], "real\n")
            # Missing refs map to "" — used for new files in the
            # tracked diff, where the merge-base side has no blob.
            self.assertEqual(contents[f"{head}:does-not-exist.txt"], "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_input_no_subprocess(self):
        with patch("actor.watch.helpers.subprocess.Popen") as p:
            result = _git_cat_file_batch([], "/tmp")
        self.assertEqual(result, {})
        p.assert_not_called()


class FileDiffCountsPlumbingTests(unittest.TestCase):
    """Stage 2 plumbs ± counts from `git diff` parse → FileDiff →
    build_diff_renderables, skipping the duplicate difflib pass."""

    def test_explicit_counts_take_precedence_over_fallback(self):
        # If caller passes counts, they're used verbatim — even when
        # they contradict the contents (e.g. a synthetic test fixture).
        fd = FileDiff(
            "x.py", old_content="a\nb\n", new_content="a\nb\nc\n",
            added=99, removed=88,
        )
        self.assertEqual(fd.added, 99)
        self.assertEqual(fd.removed, 88)

    def test_missing_counts_fall_back_to_difflib(self):
        # Backwards-compat: callers (incl. the Stage-1 test helpers)
        # that didn't pre-compute counts get them computed from the
        # contents, so the FileDiff API stays usable in tests and ad-hoc
        # debug scripts.
        fd = FileDiff("x.py", old_content="a\nb\n", new_content="a\nc\n")
        self.assertEqual(fd.added, 1)
        self.assertEqual(fd.removed, 1)

    def test_build_uses_filediff_counts_not_content(self):
        """Renderer must trust FileDiff.added/removed and not re-derive
        them. Use mismatched counts to prove no re-derivation."""
        fd = FileDiff(
            "x.py", old_content="a\n", new_content="b\n",
            # Lying counts — content shows +1/-1 but we report
            # +5/-3. The renderer must surface the reported numbers.
            added=5, removed=3,
        )
        result = build_diff_renderables([fd], dark=True)
        self.assertIsNotNone(result)
        _parts, total_added, total_removed = result
        self.assertEqual(total_added, 5)
        self.assertEqual(total_removed, 3)


# -- Stage 3: cheap-badge-first ----------------------------------------------


class ParseShortstatTests(unittest.TestCase):
    """`git diff --shortstat` summary line parser. Drives the
    near-instant DIFF (±N) badge so the user sees diff size before
    the full render commits."""

    def test_modifications(self):
        line = " 3 files changed, 7 insertions(+), 2 deletions(-)\n"
        self.assertEqual(parse_shortstat(line), (7, 2))

    def test_only_insertions(self):
        line = " 1 file changed, 1 insertion(+)\n"
        self.assertEqual(parse_shortstat(line), (1, 0))

    def test_only_deletions(self):
        line = " 1 file changed, 1 deletion(-)\n"
        self.assertEqual(parse_shortstat(line), (0, 1))

    def test_empty_input(self):
        self.assertEqual(parse_shortstat(""), (0, 0))

    def test_garbled_input_returns_zeros(self):
        # The badge is best-effort — a malformed line shouldn't crash
        # the worker, just produce zeros.
        self.assertEqual(parse_shortstat("nonsense\n"), (0, 0))


class ComputeDiffShortstatTests(unittest.TestCase):
    """End-to-end: shortstat against a real temp repo. Verifies the
    two-subprocess path (merge-base + shortstat) returns the right
    counts for a working-tree diff."""

    def test_returns_added_removed_for_modified_files(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = _build_repo(n_files=5)
        try:
            actor = _fake_actor_for_repo(tmp)
            counts = compute_diff_shortstat(actor)
            self.assertEqual(counts, (5, 5))  # 1 add + 1 del per file
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_returns_zeros_when_no_changes(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = tempfile.mkdtemp(prefix="actor-shortstat-clean-")
        try:
            _git("init", "-q", "-b", "main", tmp, cwd=tmp)
            Path(tmp, "seed.txt").write_text("seed\n")
            _git("add", "-A", cwd=tmp)
            _git("commit", "-q", "-m", "seed", cwd=tmp)
            actor = _fake_actor_for_repo(tmp)
            counts = compute_diff_shortstat(actor)
            self.assertEqual(counts, (0, 0))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_returns_none_when_not_a_repo(self):
        tmp = tempfile.mkdtemp(prefix="actor-shortstat-notrepo-")
        try:
            actor = MagicMock()
            actor.dir = tmp
            actor.base_branch = "main"
            actor.name = "x"
            self.assertIsNone(compute_diff_shortstat(actor))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ApplyDiffBadgeTests(unittest.TestCase):
    """Token discipline mirrors the build apply path: stale badge
    workers (whose token was superseded by a newer kick) drop their
    output silently."""

    def test_apply_diff_badge_drops_when_token_stale(self):
        app = _bare_app()
        app._diff_badge_token = 5
        # Older worker calls back with a stale token; nothing happens.
        with patch.object(app, "_update_diff_tab_label") as upd:
            app._apply_diff_badge(token=3, added=10, removed=2)
        upd.assert_not_called()

    def test_apply_diff_badge_updates_label_when_token_matches(self):
        app = _bare_app()
        app._diff_badge_token = 7
        with patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_badge(token=7, added=5, removed=3)
        # Label rendered with separate +/- spans (colors come from the
        # theme; assert on the plain text shape).
        self.assertEqual(app._tab_base_labels["diff"].plain, "DIFF +5 -3")


class KickDiffBadgeTests(unittest.TestCase):
    """The badge kick is independent of the build kick — it fires
    even when DIFF is hidden, doesn't depend on widget width, and
    bumps its own token counter."""

    def test_kick_bumps_token_and_starts_worker(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        with patch.object(app, "_build_diff_badge_worker") as worker:
            app._kick_diff_badge(actor)
        self.assertEqual(app._diff_badge_token, 1)
        self.assertEqual(app._diff_badge_target_actor, "alice")
        worker.assert_called_once()
        args, _kwargs = worker.call_args
        self.assertEqual(args[0], 1)
        self.assertIs(args[1], actor)

    def test_kick_fires_even_when_diff_pane_hidden(self):
        """The badge sits in the always-visible tabs bar, so it must
        update independently of which tab is currently active."""
        app = _bare_app()
        actor = _fake_actor("alice")
        # Simulate hidden DIFF pane (width 0) — Stage 1's build kick
        # would stash. The badge kick MUST NOT consult widget width.
        with patch.object(app, "query_one",
                          return_value=_scroll_with_width(0)), \
             patch.object(app, "_build_diff_badge_worker") as worker:
            app._kick_diff_badge(actor)
        worker.assert_called_once()


class BadgeBeforeBuildTests(unittest.TestCase):
    """Acceptance bar from the plan: badge appears before the full
    build commits. Both paths fire from the same kick; their
    completion ordering is independent (worker → main-thread apply
    via call_from_thread)."""

    def test_maybe_refresh_diff_kicks_both_paths(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        tree = MagicMock()
        tree.selected_actor = actor
        with patch.object(app, "query_one", return_value=tree), \
             patch.object(app, "_kick_diff_badge") as badge_kick, \
             patch.object(app, "_kick_diff_build") as build_kick:
            app._maybe_refresh_diff()
        badge_kick.assert_called_once_with(actor)
        # `force` is propagated to the build kick so live-poll-driven
        # refreshes bypass the cache-key short-circuit.
        build_kick.assert_called_once_with(actor, force=False)

    def test_badge_apply_can_land_before_build_apply(self):
        """Their tokens are independent counters, so a fast badge
        worker committing first doesn't bump the build token. The
        build's later finalizer still has its token intact and
        lands."""
        app = _bare_app()
        # Both paths kicked together.
        app._diff_badge_token = 1
        app._diff_build_token = 1
        app._diff_build_pending = True

        # Badge fires first.
        with patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_badge(token=1, added=12, removed=3)
        self.assertEqual(app._tab_base_labels["diff"].plain, "DIFF +12 -3")
        # Build path's token is untouched — it can still land.
        self.assertEqual(app._diff_build_token, 1)
        self.assertTrue(app._diff_build_pending)

        # Build's per-file streams complete and the finalizer
        # commits with its own (authoritative) counts. Last write
        # wins; the build's number includes untracked files that
        # shortstat misses.
        with patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_build_done(
                token=1,
                cache_key=("alice", "deadbeef", 100),
                total_added=14,  # diverges from badge's 12 (untracked)
                total_removed=3,
            )
        self.assertEqual(app._tab_base_labels["diff"].plain, "DIFF +14 -3")
        self.assertFalse(app._diff_build_pending)


class BadgeWorkerCancellationTests(unittest.TestCase):
    """Newer kicks supersede older badge workers via the token. The
    worker checks its token both before and after the subprocess
    work so a stale result never reaches the apply step."""

    def test_worker_bails_when_token_advances_before_shortstat(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        # Newer kick already advanced the token.
        app._diff_badge_token = 99
        with patch("actor.watch.app.compute_diff_shortstat") as ss, \
             patch.object(app, "call_from_thread") as cft:
            ActorWatchApp._build_diff_badge_worker.__wrapped__(
                app, 1, actor,  # stale token = 1, current = 99
            )
        ss.assert_not_called()
        cft.assert_not_called()

    def test_worker_bails_when_token_advances_after_shortstat(self):
        """Token can advance during the subprocess call — a faster
        kick from a tab activation, for example. The post-shortstat
        check must catch that."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_badge_token = 1
        # compute_diff_shortstat returns counts but in the meantime a
        # newer kick bumped the token.
        def shortstat_then_advance(_actor):
            app._diff_badge_token = 2
            return (5, 3)
        with patch("actor.watch.app.compute_diff_shortstat",
                   side_effect=shortstat_then_advance), \
             patch.object(app, "call_from_thread") as cft:
            ActorWatchApp._build_diff_badge_worker.__wrapped__(
                app, 1, actor,
            )
        cft.assert_not_called()

    def test_worker_skips_apply_when_shortstat_returns_none(self):
        """compute_diff_shortstat returns None on git failure (no
        repo, missing base, etc.) — the worker just returns rather
        than committing zeros, leaving any prior badge intact."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_badge_token = 1
        with patch("actor.watch.app.compute_diff_shortstat",
                   return_value=None), \
             patch.object(app, "call_from_thread") as cft:
            ActorWatchApp._build_diff_badge_worker.__wrapped__(
                app, 1, actor,
            )
        cft.assert_not_called()


# -- Stage 4: per-file streaming render -------------------------------------


class IterDiffRenderablesTests(unittest.TestCase):
    """The streaming generator yields one tuple per file as the
    worker renders ahead. The watch app's `_build_diff_worker` calls
    `call_from_thread(_diff_append_file, ...)` for each yielded tuple
    so files appear progressively rather than after one giant final
    mount."""

    def test_yields_one_tuple_per_file_in_order(self):
        from actor.watch.diff_render import iter_diff_renderables
        files = [
            FileDiff("a.py", "x = 1\n", "x = 2\n", added=1, removed=1),
            FileDiff("b.py", "", "hi\n", added=1, removed=0),
            FileDiff("c.py", "old\n", "", added=0, removed=1),
        ]
        results = list(iter_diff_renderables(files, dark=True))
        self.assertEqual(len(results), 3)
        # Streaming order matches input order — that's what the
        # consumer relies on for mount-order correctness.
        self.assertEqual([r[0] for r in results], ["a.py", "b.py", "c.py"])
        # Counts ride straight off FileDiff (Stage 2 plumbing); the
        # generator doesn't recompute them.
        self.assertEqual([(r[2], r[3]) for r in results],
                         [(1, 1), (1, 0), (0, 1)])

    def test_cancel_stops_iteration_before_next_file(self):
        from actor.watch.diff_render import iter_diff_renderables
        files = [
            FileDiff("a.py", "1\n", "2\n", added=1, removed=1),
            FileDiff("b.py", "1\n", "2\n", added=1, removed=1),
            FileDiff("c.py", "1\n", "2\n", added=1, removed=1),
        ]
        # Cancel after first yield by flipping the flag from the
        # consumer side. The generator's pre-render check kicks in
        # on the next iteration and stops cleanly.
        cancelled = {"flag": False}
        results: list = []
        for tup in iter_diff_renderables(
            files, dark=True, is_cancelled=lambda: cancelled["flag"],
        ):
            results.append(tup)
            cancelled["flag"] = True  # cancel after the first yield
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "a.py")


class DiffAppendFileTests(unittest.TestCase):
    """First append for a fresh kick clears the scroll (placeholder
    + any prior content); subsequent appends just mount. Stale
    tokens skip everything."""

    def test_first_append_clears_scroll_then_mounts(self):
        app = _bare_app()
        app._diff_build_token = 4
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll):
            app._diff_append_file(
                token=4, file_path="a.py", strips=[Strip([Segment("a")])],
            )
        scroll.remove_children.assert_called_once()
        scroll.mount.assert_called_once()
        # The mounted widget is a PrerenderedDiff carrying our strips.
        mounted = scroll.mount.call_args.args[0]
        self.assertIsInstance(mounted, PrerenderedDiff)
        self.assertEqual(app._diff_streamed_token, 4)
        # Pending must be flipped off — real content is on screen
        # so the 300ms placeholder timer must NOT fire.
        self.assertFalse(app._diff_build_pending)

    def test_subsequent_appends_skip_clear(self):
        app = _bare_app()
        app._diff_build_token = 4
        app._diff_streamed_token = 4  # first append already happened
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll):
            app._diff_append_file(
                token=4, file_path="b.py", strips=[Strip([Segment("b")])],
            )
        scroll.remove_children.assert_not_called()
        scroll.mount.assert_called_once()

    def test_stream_order_preserved_across_appends(self):
        """Mount calls land in the order the worker invoked them —
        which mirrors the iter_diff_renderables yield order, which
        mirrors compute_diff's file order."""
        app = _bare_app()
        app._diff_build_token = 1
        scroll = _scroll_with_width(100)
        mount_args: list[object] = []
        scroll.mount.side_effect = lambda widget: mount_args.append(widget)

        def fresh_strips(label: str) -> list[Strip]:
            return [Strip([Segment(label)])]

        with patch.object(app, "query_one", return_value=scroll):
            app._diff_append_file(1, "a.py", fresh_strips("A"))
            app._diff_append_file(1, "b.py", fresh_strips("B"))
            app._diff_append_file(1, "c.py", fresh_strips("C"))

        self.assertEqual(len(mount_args), 3)
        # Each mount is a PrerenderedDiff; pull the label out of the
        # first segment of its first strip.
        for widget, expected in zip(mount_args, ["A", "B", "C"]):
            self.assertIsInstance(widget, PrerenderedDiff)
            first_strip = widget._strips[0]
            self.assertEqual(first_strip._segments[0].text, expected)
        # First call also cleared the scroll once; subsequent two did
        # not.
        scroll.remove_children.assert_called_once()


class StreamingWorkerEndToEndTests(unittest.TestCase):
    """Drive `_build_diff_worker` directly with a small file set;
    verify per-file `_diff_append_file` calls happen in order, the
    finalizer runs once, and a stale token mid-stream stops the
    finalizer from committing."""

    def _make_files(self, n: int) -> list[FileDiff]:
        return [
            FileDiff(
                f"f{i}.py", f"old{i}\n", f"new{i}\n",
                added=1, removed=1,
            )
            for i in range(n)
        ]

    def test_worker_streams_then_finalizes(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_build_token = 5

        result = MagicMock()
        result.files = self._make_files(3)
        result.reason = ""

        applied: list[tuple] = []
        # Capture call_from_thread invocations to verify ordering.
        with _stub_theme(), \
             patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result), \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append(
                              (fn.__name__, a),
                          )):
            ActorWatchApp._build_diff_worker.__wrapped__(app, 5, actor, 100)

        # Three append calls in order, then exactly one finalizer.
        names = [n for n, _a in applied]
        self.assertEqual(
            names,
            ["_diff_append_file", "_diff_append_file",
             "_diff_append_file", "_apply_diff_build_done"],
            f"unexpected call sequence: {names!r}",
        )
        # The append calls landed in input file order.
        append_paths = [a[1] for n, a in applied if n == "_diff_append_file"]
        self.assertEqual(append_paths, ["f0.py", "f1.py", "f2.py"])
        # Finalizer received the aggregated counts (Stage 2 plumbing).
        finalizer_args = applied[-1][1]
        # (token, cache_key, total_added, total_removed)
        self.assertEqual(finalizer_args[0], 5)
        self.assertEqual(finalizer_args[1], ("alice", "oid", 100))
        self.assertEqual(finalizer_args[2], 3)
        self.assertEqual(finalizer_args[3], 3)

    def test_cancel_mid_stream_skips_finalizer(self):
        """Token bumped after the second file's append → worker bails
        before the finalizer. Partial mounts already on screen stay
        until the next kick's first append clears them. The cache key
        must NOT be promoted."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_build_token = 5

        result = MagicMock()
        result.files = self._make_files(5)
        result.reason = ""

        applied: list[tuple] = []

        def cft(fn, *a, **kw):
            applied.append((fn.__name__, a))
            # After the second append lands, simulate a newer kick
            # by bumping the token. The worker's post-loop
            # `is_cancelled()` check then sees the mismatch and
            # returns before finalizing.
            if len([x for x in applied if x[0] == "_diff_append_file"]) == 2:
                app._diff_build_token = 6

        with _stub_theme(), \
             patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result), \
             patch.object(app, "call_from_thread", side_effect=cft):
            ActorWatchApp._build_diff_worker.__wrapped__(app, 5, actor, 100)

        names = [n for n, _a in applied]
        # Two appends made it through before cancellation; the
        # generator's pre-render check on iteration 3 saw the bumped
        # token and stopped. No finalizer.
        self.assertEqual(names, ["_diff_append_file", "_diff_append_file"])
        # Cache key must NOT have been finalized — the next kick has
        # to re-render rather than seeing a stale "already applied".
        self.assertIsNone(app._diff_last_applied_key)

    def test_empty_file_list_routes_through_text_path(self):
        """`compute_diff` returns reason="working tree clean" with
        files=None when nothing changed. That path is text-only — no
        streaming, no finalizer, single `_apply_diff_text` mount."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_build_token = 9

        result = MagicMock()
        result.files = None
        result.reason = "working tree clean"

        applied: list[str] = []
        with patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result), \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append(
                              fn.__name__,
                          )):
            ActorWatchApp._build_diff_worker.__wrapped__(app, 9, actor, 100)
        self.assertEqual(applied, ["_apply_diff_text"])

    def test_render_exception_routes_to_error_text(self):
        """A render error mid-stream wipes the scroll and surfaces
        "Diff error: ..." in its place. `_apply_diff_text` does the
        remove_children itself, so partial appends don't linger above
        the error message."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_build_token = 1

        result = MagicMock()
        result.files = self._make_files(2)
        result.reason = ""

        applied: list[tuple] = []

        def boom(*_a, **_kw):
            raise RuntimeError("kaboom")

        with _stub_theme(), \
             patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result), \
             patch("actor.watch.app.iter_diff_renderables",
                   side_effect=boom), \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append(
                              (fn.__name__, a),
                          )):
            ActorWatchApp._build_diff_worker.__wrapped__(app, 1, actor, 100)
        names = [n for n, _a in applied]
        # Render error path now routes through `_apply_diff_error`
        # (which doesn't promote the cache key) instead of
        # `_apply_diff_text` (which did, leaving the user stuck on
        # the error mount until HEAD or width changed).
        self.assertEqual(names, ["_apply_diff_error"])
        # Reason string includes the exception message.
        self.assertIn("kaboom", applied[0][1][1])


class ClearDetailCancelsStreamingTests(unittest.TestCase):
    """Selecting nothing must wipe the diff and invalidate any
    in-flight stream — bumping the token alone is enough; the
    streaming worker's call_from_thread for the stale token is a
    no-op via the `_diff_append_file` token check."""

    def test_clear_detail_bumps_token_and_drops_pending(self):
        app = _bare_app()
        app._diff_build_token = 3
        app._diff_build_pending = True
        app._diff_streamed_token = 3
        app._diff_last_applied_key = ("alice", "x", 100)
        # _bare_app doesn't include the full DOM — substitute mocks
        # for what `_clear_detail` queries.
        info = MagicMock()
        log = MagicMock()
        log.lines = []
        table = MagicMock()
        scroll = MagicMock()

        def query_one(selector, *_args):
            if "info" in selector:
                return info
            if "logs" in selector:
                return log
            if "runs" in selector:
                return table
            return scroll

        app._log_cursors = {}
        with patch.object(app, "query_one", side_effect=query_one), \
             patch.object(app, "_update_diff_tab_label"):
            app._clear_detail()
        self.assertEqual(app._diff_build_token, 4)
        self.assertFalse(app._diff_build_pending)
        self.assertIsNone(app._diff_last_applied_key)
        # Subsequent `_diff_append_file` calls for the OLD token now
        # fall through the stale-token guard.
        scroll.reset_mock()
        with patch.object(app, "query_one", return_value=scroll):
            app._diff_append_file(token=3, file_path="x.py",
                                  strips=[Strip([Segment("x")])])
        scroll.mount.assert_not_called()


# -- Stage 5: live refresh while a run is in progress -----------------------


def _stub_diff_poll_dom(
    app, *, selected_actor=None, active_tab: str = "diff",
):
    """Return a `query_one` side-effect that resolves the two
    DOM lookups `_diff_poll_actor` makes — the ActorTree (with a
    `selected_actor` attribute) and the TabbedContent (with an
    `active` attribute). The state-machine methods only read these
    two values; everything else stays a vanilla MagicMock."""
    tree = MagicMock()
    tree.selected_actor = selected_actor
    tabs = MagicMock()
    tabs.active = active_tab

    def query_one(selector, *_args, **_kwargs):
        # ActorTree is queried by class, "#tabs" by id selector.
        if isinstance(selector, str) and selector == "#tabs":
            return tabs
        return tree

    return query_one


class GitIndexMtimeTests(unittest.TestCase):
    """The cheap "did anything happen" signal — works for primary
    checkouts AND for `git worktree` checkouts where `.git` is a
    file pointing at the per-worktree git dir."""

    def test_returns_mtime_for_primary_checkout(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = tempfile.mkdtemp(prefix="actor-index-primary-")
        try:
            _git("init", "-q", "-b", "main", tmp, cwd=tmp)
            Path(tmp, "seed.txt").write_text("seed\n")
            _git("add", "-A", cwd=tmp)
            _git("commit", "-q", "-m", "seed", cwd=tmp)
            actor = _fake_actor_for_repo(tmp)
            mtime = git_index_mtime(actor)
            self.assertIsNotNone(mtime)
            self.assertIsInstance(mtime, float)
            # Should match the actual file's mtime.
            self.assertEqual(mtime, Path(tmp, ".git", "index").stat().st_mtime)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_returns_mtime_for_worktree_checkout(self):
        """`actor new` typically lands as a `git worktree`, so
        `<wt>/.git` is a *file* with `gitdir: <path>` pointing at the
        per-worktree git dir under the parent repo."""
        if shutil.which("git") is None:
            self.skipTest("git not available")
        primary = tempfile.mkdtemp(prefix="actor-index-primary-for-wt-")
        wt = tempfile.mkdtemp(prefix="actor-index-wt-")
        os.rmdir(wt)  # `git worktree add` creates it
        try:
            _git("init", "-q", "-b", "main", primary, cwd=primary)
            Path(primary, "seed.txt").write_text("seed\n")
            _git("add", "-A", cwd=primary)
            _git("commit", "-q", "-m", "seed", cwd=primary)
            _git("worktree", "add", wt, "-b", "feature", cwd=primary)
            actor = _fake_actor_for_repo(wt)
            mtime = git_index_mtime(actor)
            self.assertIsNotNone(mtime)
            self.assertIsInstance(mtime, float)
        finally:
            shutil.rmtree(wt, ignore_errors=True)
            shutil.rmtree(primary, ignore_errors=True)

    def test_returns_none_when_not_a_repo(self):
        tmp = tempfile.mkdtemp(prefix="actor-index-notrepo-")
        try:
            actor = _fake_actor_for_repo(tmp)
            self.assertIsNone(git_index_mtime(actor))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DiffPollActorTests(unittest.TestCase):
    """`_diff_poll_actor` returns the eligible actor only when ALL
    conditions hold: actor selected, RUNNING status, DIFF active."""

    def test_returns_actor_when_running_and_diff_active(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.RUNNING}
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ):
            self.assertIs(app._diff_poll_actor(), actor)

    def test_returns_none_when_no_actor_selected(self):
        app = _bare_app()
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=None, active_tab="diff",
            ),
        ):
            self.assertIsNone(app._diff_poll_actor())

    def test_returns_none_when_actor_not_running(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.DONE}
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ):
            self.assertIsNone(app._diff_poll_actor())

    def test_returns_none_when_diff_tab_inactive(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.RUNNING}
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="logs",
            ),
        ):
            self.assertIsNone(app._diff_poll_actor())


class PollDiffForRunningTests(unittest.TestCase):
    """The interval handler. Only spawns the signal-capture worker
    when conditions hold; resets baseline state otherwise so a
    re-entry doesn't compare against stale signals."""

    def test_starts_worker_when_conditions_met(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.RUNNING}
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ), patch.object(app, "_poll_diff_signals_worker") as worker:
            app._poll_diff_for_running()
        worker.assert_called_once_with(actor)

    def test_resets_state_when_conditions_not_met(self):
        app = _bare_app()
        # Pretend a prior tick had observed signals.
        app._diff_poll_initialized = True
        app._diff_poll_last_index_mtime = 1.0
        app._diff_poll_last_shortstat = (3, 1)
        # No actor selected → conditions fail.
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=None,
            ),
        ), patch.object(app, "_poll_diff_signals_worker") as worker:
            app._poll_diff_for_running()
        worker.assert_not_called()
        self.assertFalse(app._diff_poll_initialized)
        self.assertIsNone(app._diff_poll_last_index_mtime)
        self.assertIsNone(app._diff_poll_last_shortstat)

    def test_resets_state_when_actor_not_running(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.DONE}
        app._diff_poll_initialized = True
        app._diff_poll_last_index_mtime = 1.0
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ), patch.object(app, "_poll_diff_signals_worker") as worker:
            app._poll_diff_for_running()
        worker.assert_not_called()
        self.assertFalse(app._diff_poll_initialized)

    def test_resets_state_when_tab_inactive(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.RUNNING}
        app._diff_poll_initialized = True
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="logs",
            ),
        ), patch.object(app, "_poll_diff_signals_worker") as worker:
            app._poll_diff_for_running()
        worker.assert_not_called()
        self.assertFalse(app._diff_poll_initialized)


class EvaluateDiffPollSignalsTests(unittest.TestCase):
    """Main-thread signal comparison. First observation baselines;
    subsequent ticks fire `_maybe_refresh_diff(force=True)` if either
    signal flipped."""

    def _running_actor(self, app, actor):
        app._prev_statuses = {actor.name: Status.RUNNING}
        return _stub_diff_poll_dom(
            app, selected_actor=actor, active_tab="diff",
        )

    def test_first_observation_baselines_no_refresh(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        with patch.object(
            app, "query_one", side_effect=self._running_actor(app, actor),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=10.0, shortstat=(3, 1), untracked=2,
            )
        refresh.assert_not_called()
        self.assertTrue(app._diff_poll_initialized)
        self.assertEqual(app._diff_poll_last_actor, "alice")
        self.assertEqual(app._diff_poll_last_index_mtime, 10.0)
        self.assertEqual(app._diff_poll_last_shortstat, (3, 1))
        self.assertEqual(app._diff_poll_last_untracked, 2)

    def test_unchanged_signals_no_refresh(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_poll_initialized = True
        app._diff_poll_last_actor = "alice"
        app._diff_poll_last_index_mtime = 10.0
        app._diff_poll_last_shortstat = (3, 1)
        app._diff_poll_last_untracked = 0
        with patch.object(
            app, "query_one", side_effect=self._running_actor(app, actor),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=10.0, shortstat=(3, 1), untracked=0,
            )
        refresh.assert_not_called()

    def test_index_mtime_change_triggers_refresh(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_poll_initialized = True
        app._diff_poll_last_actor = "alice"
        app._diff_poll_last_index_mtime = 10.0
        app._diff_poll_last_shortstat = (3, 1)
        app._diff_poll_last_untracked = 0
        with patch.object(
            app, "query_one", side_effect=self._running_actor(app, actor),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=20.0, shortstat=(3, 1), untracked=0,
            )
        refresh.assert_called_once_with(force=True)
        # Baseline advanced.
        self.assertEqual(app._diff_poll_last_index_mtime, 20.0)

    def test_shortstat_change_triggers_refresh(self):
        """Picks up unstaged edits the index doesn't reflect — the
        whole reason we pair the two signals."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_poll_initialized = True
        app._diff_poll_last_actor = "alice"
        app._diff_poll_last_index_mtime = 10.0
        app._diff_poll_last_shortstat = (3, 1)
        app._diff_poll_last_untracked = 0
        with patch.object(
            app, "query_one", side_effect=self._running_actor(app, actor),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=10.0, shortstat=(5, 2), untracked=0,
            )
        refresh.assert_called_once_with(force=True)
        self.assertEqual(app._diff_poll_last_shortstat, (5, 2))

    def test_untracked_count_change_triggers_refresh(self):
        """Stage-5 third signal: a new untracked file (the most
        common output shape from an actor's `Write` tool) doesn't
        flip mtime or shortstat, so without `untracked` the live
        refresh would never fire on file creation."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_poll_initialized = True
        app._diff_poll_last_actor = "alice"
        app._diff_poll_last_index_mtime = 10.0
        app._diff_poll_last_shortstat = (3, 1)
        app._diff_poll_last_untracked = 0
        with patch.object(
            app, "query_one", side_effect=self._running_actor(app, actor),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=10.0, shortstat=(3, 1), untracked=1,
            )
        refresh.assert_called_once_with(force=True)
        self.assertEqual(app._diff_poll_last_untracked, 1)

    def test_actor_changed_mid_poll_drops_signals(self):
        """Signals captured for `alice` mustn't refresh `bob`'s
        diff. The worker can race with a tab/actor change between
        the kick and the call_from_thread."""
        app = _bare_app()
        bob = _fake_actor("bob")
        app._prev_statuses = {"bob": Status.RUNNING}
        # Conditions hold for `bob` now.
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=bob, active_tab="diff",
            ),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            # ...but the worker is reporting signals captured for
            # `alice` before the tab switch.
            app._evaluate_diff_poll_signals(
                "alice", mtime=99.0, shortstat=(7, 7), untracked=5,
            )
        refresh.assert_not_called()
        # State must NOT advance to `alice`'s captured signals —
        # those would corrupt `bob`'s baseline.
        self.assertFalse(app._diff_poll_initialized)
        self.assertIsNone(app._diff_poll_last_index_mtime)

    def test_actor_switch_re_baselines_without_refresh(self):
        """When the selected actor changes between polls, the next
        evaluation must re-baseline rather than compare bob's
        signals against alice's stale baseline (which would always
        diverge and fire a redundant force-refresh on top of the
        actor-switch kick `on_tree_node_highlighted` already
        triggered)."""
        app = _bare_app()
        bob = _fake_actor("bob")
        app._prev_statuses = {"bob": Status.RUNNING}
        # Baseline was for alice, but the user has switched to bob.
        app._diff_poll_initialized = True
        app._diff_poll_last_actor = "alice"
        app._diff_poll_last_index_mtime = 10.0
        app._diff_poll_last_shortstat = (3, 1)
        app._diff_poll_last_untracked = 0
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=bob, active_tab="diff",
            ),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "bob", mtime=99.0, shortstat=(7, 7), untracked=5,
            )
        # No refresh — bob's first observation is a baseline.
        refresh.assert_not_called()
        self.assertEqual(app._diff_poll_last_actor, "bob")
        self.assertEqual(app._diff_poll_last_index_mtime, 99.0)
        self.assertEqual(app._diff_poll_last_shortstat, (7, 7))
        self.assertEqual(app._diff_poll_last_untracked, 5)

    def test_drops_signals_when_conditions_no_longer_hold(self):
        """User switched to LIVE mid-poll; signals stale on arrival."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.RUNNING}
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="logs",
            ),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=10.0, shortstat=(3, 1), untracked=0,
            )
        refresh.assert_not_called()


class DiffPollResetCleanlyTests(unittest.TestCase):
    """End-to-end stop-cleanly behavior. After a tick where
    conditions become false, the next tick where they become true
    again must re-baseline rather than refresh on stale comparison."""

    def test_re_entry_after_reset_treats_as_first_observation(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.RUNNING}

        # First tick: conditions met. Baseline records.
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=10.0, shortstat=(3, 1), untracked=0,
            )
        refresh.assert_not_called()
        self.assertTrue(app._diff_poll_initialized)

        # User leaves DIFF for LIVE. Interval still ticks.
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="logs",
            ),
        ), patch.object(app, "_poll_diff_signals_worker") as worker:
            app._poll_diff_for_running()
        worker.assert_not_called()
        self.assertFalse(app._diff_poll_initialized)
        self.assertIsNone(app._diff_poll_last_index_mtime)

        # User comes back to DIFF. Even if signals differ from
        # what was last seen *long ago*, the next eval is a fresh
        # baseline — no surprise refresh.
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ), patch.object(app, "_maybe_refresh_diff") as refresh:
            app._evaluate_diff_poll_signals(
                "alice", mtime=99.0, shortstat=(99, 99), untracked=99,
            )
        refresh.assert_not_called()
        self.assertTrue(app._diff_poll_initialized)
        self.assertEqual(app._diff_poll_last_index_mtime, 99.0)


# -- CR-loop fixes: regression tests ---------------------------------------


class StreamCancelInvalidatesCacheTests(unittest.TestCase):
    """When a streaming build is cancelled mid-flight after at least
    one file mounted, partial content remains on screen but the
    cache key cannot stay promoted from the *previous* successful
    build — otherwise a subsequent kick at the same key would
    cache-hit and leave the user staring at the partial state
    forever (e.g. resize away then back to a previously-cached width
    while a re-render was in flight)."""

    def test_cancel_after_first_append_invalidates_cache_key(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        # Cache holds a key from a *different* prior build (different
        # head_oid). The worker's cache check shouldn't early-out;
        # we want the worker to actually start streaming so we can
        # observe the mid-stream cancellation path.
        app._diff_last_applied_key = ("alice", "different-oid", 100)
        app._diff_build_token = 5
        app._diff_streamed_token = -1

        result = MagicMock()
        result.files = [
            FileDiff(f"f{i}.py", f"old{i}\n", f"new{i}\n", added=1, removed=1)
            for i in range(3)
        ]
        result.reason = ""

        applied: list[tuple] = []

        def cft(fn, *a, **kw):
            applied.append((fn.__name__, a))
            # `_diff_append_file` would set `_diff_streamed_token` on
            # the main thread; emulate that here so
            # `_on_stream_cancelled` sees streamed_token == token.
            if fn.__name__ == "_diff_append_file":
                app._diff_streamed_token = a[0]
            # After the first append lands, simulate a newer kick.
            if len([x for x in applied if x[0] == "_diff_append_file"]) == 1:
                app._diff_build_token = 6

        with _stub_theme(), \
             patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result), \
             patch.object(app, "call_from_thread", side_effect=cft):
            ActorWatchApp._build_diff_worker.__wrapped__(
                app, 5, actor, 100, False,
            )

        # Worker bailed via _on_stream_cancelled, which scheduled
        # _invalidate_diff_cache_key on the main thread.
        names = [n for n, _a in applied]
        self.assertIn("_invalidate_diff_cache_key", names)
        # And finalizer did NOT run (no cache-key promotion of the
        # cancelled build).
        self.assertNotIn("_apply_diff_build_done", names)


class ForceRefreshBypassesCacheTests(unittest.TestCase):
    """Live poll fires `_maybe_refresh_diff(force=True)` precisely
    BECAUSE the worktree changed even though HEAD didn't move. The
    cache key is keyed on (actor, head_oid, width), so without a
    bypass the worker would always cache-hit and the live refresh
    would never repaint."""

    def test_force_true_skips_cache_hit_short_circuit(self):
        app = _bare_app()
        # Cache is populated with the exact key the worker will
        # compute next.
        app._diff_last_applied_key = ("alice", "oid", 100)
        actor = _fake_actor("alice")
        app._diff_build_token = 11

        result = MagicMock()
        result.files = None
        result.reason = "working tree clean"
        applied: list[tuple] = []
        with _stub_theme(), \
             patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result) as compute, \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append(
                              (fn.__name__, a),
                          )):
            ActorWatchApp._build_diff_worker.__wrapped__(
                app, 11, actor, 100, True,  # force=True
            )
        # compute_diff RAN despite the cache key matching.
        compute.assert_called_once()
        # And _apply_diff_text was called (no files reason path),
        # NOT _mark_diff_build_done (cache hit early-out).
        names = [n for n, _a in applied]
        self.assertEqual(names, ["_apply_diff_text"])

    def test_force_false_keeps_cache_hit_short_circuit(self):
        """Sanity: non-force kicks still cache-hit when the key
        matches. Otherwise the cache layer is dead weight."""
        app = _bare_app()
        app._diff_last_applied_key = ("alice", "oid", 100)
        actor = _fake_actor("alice")
        app._diff_build_token = 11

        applied: list[str] = []
        with _stub_theme(), \
             patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff") as compute, \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append(
                              fn.__name__,
                          )):
            ActorWatchApp._build_diff_worker.__wrapped__(
                app, 11, actor, 100, False,  # force=False
            )
        compute.assert_not_called()
        self.assertEqual(applied, ["_mark_diff_build_done"])


class ApplyDiffErrorTests(unittest.TestCase):
    """Render-error path is split out from `_apply_diff_text` so
    error states don't poison the cache and lock the user out of
    retries until HEAD or width changes."""

    def test_error_clears_pending_and_invalidates_cache_key(self):
        app = _bare_app()
        app._diff_build_token = 7
        app._diff_build_pending = True
        # A previous successful build cached a key.
        app._diff_last_applied_key = ("alice", "oid", 100)
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll), \
             patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_error(token=7, text="Diff error: kaboom")
        scroll.remove_children.assert_called_once()
        scroll.mount.assert_called_once()
        self.assertFalse(app._diff_build_pending)
        # Cache key dropped — next non-force kick at the same key
        # rebuilds rather than hitting the stale cache.
        self.assertIsNone(app._diff_last_applied_key)

    def test_error_uses_markup_false_static(self):
        """Exception messages can contain `[red]`-like substrings
        that Rich's default markup parser would interpret. Mounting
        with `markup=False` keeps the text literal."""
        from textual.widgets import Static
        app = _bare_app()
        app._diff_build_token = 1
        scroll = MagicMock()
        scroll.size = MagicMock()
        scroll.size.width = 100
        with patch.object(app, "query_one", return_value=scroll), \
             patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_error(token=1, text="error: [red]boom[/red]")
        widget = scroll.mount.call_args.args[0]
        self.assertIsInstance(widget, Static)
        # Textual's Static stores the markup flag as `_render_markup`.
        self.assertFalse(widget._render_markup)


class ParseDiffFilesRenameTests(unittest.TestCase):
    """Renames need the OLD path to look up the pre-image at the
    merge base — querying the new path in that tree would miss
    because the rename hadn't happened yet, causing the diff to
    render as if the entire file was newly added."""

    def test_rename_with_modifications_captures_old_path(self):
        diff = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 87%\n"
            "rename from old.py\n"
            "rename to new.py\n"
            "index abc..def 100644\n"
            "--- a/old.py\n"
            "+++ b/new.py\n"
            "@@ -1,3 +1,3 @@\n"
            " ctx\n"
            "-removed\n"
            "+added\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "new.py")
        self.assertEqual(files[0]["old_path"], "old.py")
        self.assertEqual(files[0]["added"], 1)
        self.assertEqual(files[0]["removed"], 1)

    def test_pure_rename_no_content_change_still_captures_paths(self):
        diff = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 100%\n"
            "rename from old.py\n"
            "rename to new.py\n"
        )
        files = _parse_diff_files(diff)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "new.py")
        self.assertEqual(files[0]["old_path"], "old.py")

    def test_compute_diff_uses_old_path_for_rename_lookup(self):
        """End-to-end: a renamed file's pre-image is read from the
        merge base via its OLD name. Without the fix, cat-file
        would query `<base>:new.py` (missing) and the diff would
        render as a wholly-new file."""
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = tempfile.mkdtemp(prefix="actor-rename-")
        try:
            _git("init", "-q", "-b", "main", tmp, cwd=tmp)
            # 10-line file so a 1-line modification leaves >90%
            # similarity — well above git's default rename detection
            # threshold (50%).
            seed = "\n".join(f"line {i}" for i in range(10)) + "\n"
            Path(tmp, "old.py").write_text(seed)
            _git("add", "-A", cwd=tmp)
            _git("commit", "-q", "-m", "seed", cwd=tmp)
            _git("mv", "old.py", "new.py", cwd=tmp)
            # Modify just one line so git's rename detection still
            # pairs the files together via similarity heuristic.
            modified = seed.replace("line 5\n", "LINE FIVE\n")
            Path(tmp, "new.py").write_text(modified)
            actor = _fake_actor_for_repo(tmp)
            result = compute_diff(actor)
            self.assertIsNotNone(result.files)
            self.assertEqual(len(result.files), 1)
            fd = result.files[0]
            self.assertEqual(fd.file_path, "new.py")
            # Pre-image is the OLD content — captured via the rename
            # header. Without the fix this would have been "".
            self.assertEqual(fd.old_content, seed)
            self.assertEqual(fd.new_content, modified)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ParseDiffFilesQuotedPathTests(unittest.TestCase):
    """`compute_diff` pins `core.quotepath=false` so the parser sees
    raw utf-8 paths instead of git's default octal-escaped quoted
    form. Verify the diff command includes the flag."""

    def test_compute_diff_passes_core_quotepath_false(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = _build_repo(n_files=1)
        try:
            actor = _fake_actor_for_repo(tmp)
            counter = _SubprocessCounter()
            with patch("actor.watch.helpers.subprocess", counter):
                compute_diff(actor)
            diff_calls = [
                c for c in counter.calls
                if c[:1] == ["git"] and "diff" in c
            ]
            # The diff invocation includes `-c core.quotepath=false`.
            self.assertTrue(
                any(
                    c[1:3] == ["-c", "core.quotepath=false"]
                    and "diff" in c
                    for c in diff_calls
                ),
                f"expected -c core.quotepath=false in diff invocation; "
                f"got {diff_calls!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WordDiffLowSimilarityTests(unittest.TestCase):
    """When two paired lines are too dissimilar (>40% changed),
    the word-diff path produces a uniform `*_word_bg` smear instead
    of useful highlighting. Returning None tells the caller to fall
    through to the standard syntax-highlighted line-bg path."""

    def test_returns_none_on_low_similarity(self):
        from actor.watch.diff_render import _word_diff
        # Two completely different lines — ratio = 0.
        result = _word_diff(
            "this is the old line", "totally different content here",
        )
        self.assertIsNone(result)

    def test_returns_tokens_on_high_similarity(self):
        from actor.watch.diff_render import _word_diff
        # Single-token change in a 5-token line — ratio ~0.8.
        result = _word_diff(
            "first second third fourth fifth",
            "first second THIRD fourth fifth",
        )
        self.assertIsNotNone(result)
        old_tokens, new_tokens = result
        # Tokenization splits on whitespace runs. Verify per-token
        # changed flags exist.
        self.assertTrue(any(changed for _t, changed in old_tokens))
        self.assertTrue(any(changed for _t, changed in new_tokens))


class GitCatFileBatchCleanupTests(unittest.TestCase):
    """The `with subprocess.Popen(...)` context manager guarantees
    pipe + child cleanup even if `communicate()` raises mid-call.
    The previous bare try/except leaked the child + three pipes."""

    def test_communicate_exception_does_not_leak(self):
        from actor.watch.helpers import _git_cat_file_batch
        # Construct a Popen mock whose communicate raises and whose
        # __exit__ records the cleanup.
        proc_mock = MagicMock()
        proc_mock.communicate.side_effect = OSError("boom")
        cm_mock = MagicMock()
        cm_mock.__enter__ = MagicMock(return_value=proc_mock)
        cm_mock.__exit__ = MagicMock(return_value=False)
        with patch("actor.watch.helpers.subprocess.Popen", return_value=cm_mock):
            result = _git_cat_file_batch(["abc:foo"], "/tmp")
        # All requested refs map to "" on failure.
        self.assertEqual(result, {"abc:foo": ""})
        # The context manager's __exit__ ran, ensuring cleanup of
        # the child process and its pipes.
        cm_mock.__exit__.assert_called_once()


# -- CR-loop fixes Round 2 ---------------------------------------------------


class InteractivePollEligibilityTests(unittest.TestCase):
    """`Status.INTERACTIVE` is a display-only overlay applied to
    actors with a live interactive session; under the hood they're
    typically also RUNNING. Excluding them from live-poll eligibility
    silently disables the live diff refresh for one of the most common
    use cases — a user driving an agent interactively and watching its
    edits land. Both states must qualify."""

    def test_interactive_status_is_eligible_for_polling(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.INTERACTIVE}
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ):
            self.assertIs(app._diff_poll_actor(), actor)

    def test_done_status_is_not_eligible(self):
        """Sanity: only RUNNING and INTERACTIVE qualify; idle/done
        actors don't get auto-refresh (their worktree shouldn't be
        changing)."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._prev_statuses = {"alice": Status.DONE}
        with patch.object(
            app, "query_one",
            side_effect=_stub_diff_poll_dom(
                app, selected_actor=actor, active_tab="diff",
            ),
        ):
            self.assertIsNone(app._diff_poll_actor())


class ComputeDiffErrorRoutingTests(unittest.TestCase):
    """`compute_diff` returns `DiffResult(reason="error")` on
    catch-all failures. That path must NOT cache the result —
    otherwise a transient git failure poisons the cache and locks
    the user out of retries until HEAD or width changes. The worker
    routes "error" reasons through `_apply_diff_error` (which
    invalidates the cache key); benign reasons still go through
    `_apply_diff_text` (which caches)."""

    def test_error_reason_routes_to_apply_diff_error(self):
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_build_token = 3

        result = MagicMock()
        result.files = None
        result.reason = "error"

        applied: list[tuple] = []
        with patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result), \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append(
                              (fn.__name__, a),
                          )):
            ActorWatchApp._build_diff_worker.__wrapped__(
                app, 3, actor, 100, False,
            )
        names = [n for n, _a in applied]
        self.assertEqual(names, ["_apply_diff_error"])

    def test_benign_reason_still_routes_to_apply_diff_text(self):
        """Sanity: non-error reasons (e.g. "working tree clean",
        "no repository") still cache via `_apply_diff_text`."""
        app = _bare_app()
        actor = _fake_actor("alice")
        app._diff_build_token = 3

        result = MagicMock()
        result.files = None
        result.reason = "working tree clean"

        applied: list[tuple] = []
        with patch("actor.watch.app.read_head_oid", return_value="oid"), \
             patch("actor.watch.app.compute_diff", return_value=result), \
             patch.object(app, "call_from_thread",
                          side_effect=lambda fn, *a, **kw: applied.append(
                              (fn.__name__, a),
                          )):
            ActorWatchApp._build_diff_worker.__wrapped__(
                app, 3, actor, 100, False,
            )
        names = [n for n, _a in applied]
        self.assertEqual(names, ["_apply_diff_text"])


class PureRenameStillVisibleTests(unittest.TestCase):
    """A rename with similarity=100% has identical content on both
    sides. The previous `if old != new: append` filter dropped these
    silently, leading the diff view to display "working tree clean"
    when the user can plainly see in `git status` that something
    changed. Renames must still surface as a FileDiff entry."""

    def test_pure_rename_appears_in_compute_diff(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = tempfile.mkdtemp(prefix="actor-rename-pure-")
        try:
            _git("init", "-q", "-b", "main", tmp, cwd=tmp)
            seed = "\n".join(f"line {i}" for i in range(10)) + "\n"
            Path(tmp, "old.py").write_text(seed)
            _git("add", "-A", cwd=tmp)
            _git("commit", "-q", "-m", "seed", cwd=tmp)
            # Pure rename — git mv with no content modification.
            _git("mv", "old.py", "new.py", cwd=tmp)
            actor = _fake_actor_for_repo(tmp)
            result = compute_diff(actor)
            self.assertIsNotNone(
                result.files,
                f"pure rename should surface in the diff (reason={result.reason!r})",
            )
            self.assertEqual(len(result.files), 1)
            fd = result.files[0]
            self.assertEqual(fd.file_path, "new.py")
            # Content matches on both sides for a pure rename.
            self.assertEqual(fd.old_content, fd.new_content)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BuildInvalidatesBadgeTokenTests(unittest.TestCase):
    """The build's count is authoritative (it includes untracked
    files; shortstat doesn't). When a build commits its label, any
    in-flight badge worker from the same kick must be invalidated
    so it can't later overwrite with a smaller, less-correct count."""

    def test_apply_diff_build_done_bumps_badge_token(self):
        app = _bare_app()
        app._diff_build_token = 5
        app._diff_badge_token = 5
        with patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_build_done(
                token=5,
                cache_key=("alice", "oid", 100),
                total_added=10,
                total_removed=2,
            )
        # Badge token bumped past 5 → any in-flight badge worker
        # whose `_apply_diff_badge(token=5, ...)` arrives later
        # will see a token mismatch and skip.
        self.assertGreater(app._diff_badge_token, 5)

    def test_apply_diff_text_bumps_badge_token(self):
        """Same guard for the cacheable text-mount path: an
        in-flight badge worker shouldn't overwrite a 'working tree
        clean' label with a shortstat-derived count."""
        app = _bare_app()
        app._diff_build_token = 5
        app._diff_badge_token = 5
        scroll = _scroll_with_width(100)
        with patch.object(app, "query_one", return_value=scroll), \
             patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_text(
                token=5,
                cache_key=("alice", "oid", 100),
                text="working tree clean",
            )
        self.assertGreater(app._diff_badge_token, 5)

    def test_late_badge_drops_after_build_apply(self):
        """End-to-end: build commits first, then a late-arriving
        badge for the same kick is rejected by the token check
        rather than clobbering the build's label."""
        app = _bare_app()
        app._diff_build_token = 5
        app._diff_badge_token = 5

        # Build applies first with authoritative count.
        with patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_build_done(
                token=5,
                cache_key=("alice", "oid", 100),
                total_added=12,
                total_removed=3,
            )
        # The label is a Rich Text using the green +N / red -N pill
        # style (a Stage-3 follow-up); the displayed numbers come
        # from `total_added` / `total_removed`.
        label_after_build = str(app._tab_base_labels["diff"])
        self.assertIn("+12", label_after_build)
        self.assertIn("-3", label_after_build)

        # Late badge from the same kick (token=5) tries to apply
        # with shortstat-only count (e.g. missing untracked).
        # It must be dropped — its token is now stale because the
        # build apply bumped `_diff_badge_token`.
        with patch.object(app, "_refresh_tab_arrows"):
            app._apply_diff_badge(token=5, added=8, removed=3)
        # Label still reflects the build's authoritative count,
        # not the badge's smaller one.
        label_after_badge = str(app._tab_base_labels["diff"])
        self.assertIn("+12", label_after_badge)
        self.assertNotIn("+8", label_after_badge)


class GitLocaleEnvTests(unittest.TestCase):
    """`compute_diff` and `compute_diff_shortstat` parse stderr
    looking for "fatal: not a git repository". Under non-English
    locales (`LC_MESSAGES=de_DE.UTF-8`, etc.) git emits translated
    messages and the substring check fails. Pinning `LC_ALL=C` on
    every git invocation that we read stderr from keeps the
    detection robust."""

    def test_compute_diff_passes_lc_all_c(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = _build_repo(n_files=1)
        try:
            actor = _fake_actor_for_repo(tmp)
            captured_envs: list = []
            real_run = subprocess.run

            def capture(args, *a, **kw):
                captured_envs.append(kw.get("env"))
                return real_run(args, *a, **kw)

            with patch("actor.watch.helpers.subprocess.run",
                       side_effect=capture):
                compute_diff(actor)
            # At least one git call carried `LC_ALL=C`.
            self.assertTrue(
                any(
                    env is not None and env.get("LC_ALL") == "C"
                    for env in captured_envs
                ),
                f"expected at least one git call with LC_ALL=C; "
                f"got envs {[e and e.get('LC_ALL') for e in captured_envs]!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_compute_diff_shortstat_passes_lc_all_c(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        tmp = _build_repo(n_files=1)
        try:
            actor = _fake_actor_for_repo(tmp)
            captured_envs: list = []
            real_run = subprocess.run

            def capture(args, *a, **kw):
                captured_envs.append(kw.get("env"))
                return real_run(args, *a, **kw)

            with patch("actor.watch.helpers.subprocess.run",
                       side_effect=capture):
                compute_diff_shortstat(actor)
            self.assertTrue(
                all(
                    env is not None and env.get("LC_ALL") == "C"
                    for env in captured_envs
                ),
                f"every shortstat git call should carry LC_ALL=C; "
                f"got envs {[e and e.get('LC_ALL') for e in captured_envs]!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
