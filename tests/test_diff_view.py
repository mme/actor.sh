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

from rich.text import Text

from actor.watch.app import ActorWatchApp
from actor.watch.diff_render import build_diff_renderables
from actor.watch.helpers import (
    FileDiff,
    _git_cat_file_batch,
    _parse_diff_files,
    compute_diff,
    compute_diff_shortstat,
    parse_shortstat,
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
    app._diff_badge_token = 0
    app._diff_badge_target_actor = None
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
                renderable=Text("hi"),
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
        # Label rendered with the combined ± total.
        self.assertEqual(app._tab_base_labels["diff"], "DIFF (±8)")


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
        build_kick.assert_called_once_with(actor)

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
        self.assertEqual(app._tab_base_labels["diff"], "DIFF (±15)")
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
        self.assertEqual(app._tab_base_labels["diff"], "DIFF (±17)")
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
                token=4, file_path="a.py", renderable=Text("rendered a"),
            )
        scroll.remove_children.assert_called_once()
        scroll.mount.assert_called_once()
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
                token=4, file_path="b.py", renderable=Text("b"),
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

        def fresh_renderable(label: str) -> Text:
            return Text(label)

        with patch.object(app, "query_one", return_value=scroll):
            app._diff_append_file(1, "a.py", fresh_renderable("A"))
            app._diff_append_file(1, "b.py", fresh_renderable("B"))
            app._diff_append_file(1, "c.py", fresh_renderable("C"))

        self.assertEqual(len(mount_args), 3)
        # Each mount holds a Static wrapping a Group(renderable, Text("")).
        # The label letter sits inside the Group's first child.
        for widget, expected in zip(mount_args, ["A", "B", "C"]):
            # widget is Static; Static.content is the Group; the first
            # element of the Group is our supplied Text.
            inner = widget.content
            self.assertEqual(inner.renderables[0].plain, expected)
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
        self.assertEqual(names, ["_apply_diff_text"])
        # Reason string includes the exception message.
        self.assertIn("kaboom", applied[0][1][2])


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
                                  renderable=Text("x"))
        scroll.mount.assert_not_called()


if __name__ == "__main__":
    unittest.main()
