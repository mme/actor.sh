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
from pathlib import Path
from unittest.mock import MagicMock, patch

from actor.watch.app import ActorWatchApp
from actor.watch.diff_render import build_diff_renderables
from actor.watch.helpers import (
    FileDiff,
    _git_cat_file_batch,
    _parse_diff_files,
    compute_diff,
)


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


if __name__ == "__main__":
    unittest.main()
