"""Run-to-LogEntry correlation via byte offsets.

Covers four layers:
 - the offset-aware line splitter primitives in ``_jsonl``,
 - ``source_offset`` propagation from file bytes to ``LogEntry``
   across Claude and Codex,
 - run-boundary offset capture in ``cmd_run`` / ``cmd_stop``,
 - the ``compute_run_ranges`` / ``bucket_entries_by_run`` layer
   (including open interactive runs and pre-feature rows).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from actor.agents._jsonl import (
    iter_lines_with_offsets,
    split_complete_lines_with_offsets,
)
from actor.agents.claude import ClaudeAgent
from actor.agents.codex import CodexAgent
from actor.db import Database
from actor.interfaces import LogEntry, LogEntryKind
from actor.run_correlation import (
    bucket_entries_by_run,
    compute_run_ranges,
)
from actor.types import ActorConfig, Run, Status


# -- Splitter primitives ------------------------------------------------------

class TestSplitCompleteLinesWithOffsets(unittest.TestCase):
    def test_offsets_are_absolute_via_base(self):
        # Two complete lines starting at byte positions 0 and 4
        # within `data`; base_offset shifts both by the same amount.
        data = b"aaa\nbb\n"
        lines, advance = split_complete_lines_with_offsets(data, base_offset=100)
        self.assertEqual(lines, [(100, "aaa"), (104, "bb")])
        self.assertEqual(advance, 7)

    def test_partial_tail_dropped(self):
        data = b"first\npart"
        lines, advance = split_complete_lines_with_offsets(data, 0)
        self.assertEqual(lines, [(0, "first")])
        self.assertEqual(advance, 6)

    def test_no_newline_defers_entirely(self):
        data = b"incomplete"
        lines, advance = split_complete_lines_with_offsets(data, 0)
        self.assertEqual(lines, [])
        self.assertIsNone(advance)

    def test_blank_lines_preserved_with_offsets(self):
        # Blank lines matter for byte accounting even though the
        # downstream JSON parser will skip them.
        data = b"a\n\nb\n"
        lines, advance = split_complete_lines_with_offsets(data, 0)
        self.assertEqual(lines, [(0, "a"), (2, ""), (3, "b")])
        self.assertEqual(advance, 5)


class TestIterLinesWithOffsets(unittest.TestCase):
    def test_includes_unterminated_final_line(self):
        # Full-read path must see a final line even if the file was
        # never flushed with a trailing newline.
        data = b"one\ntwo"
        self.assertEqual(
            list(iter_lines_with_offsets(data, 0)),
            [(0, "one"), (4, "two")],
        )

    def test_empty_data_yields_nothing(self):
        self.assertEqual(list(iter_lines_with_offsets(b"", 0)), [])


# -- source_offset propagation ------------------------------------------------

class TestClaudeSourceOffset(unittest.TestCase):
    def _setup(self) -> tuple[ClaudeAgent, Path, str, Path]:
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        agent = ClaudeAgent()
        dir_p = Path(home) / "work"
        dir_p.mkdir()
        session = "abc-123"
        path = agent._session_file_path(dir_p, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return agent, dir_p, session, path

    def test_full_read_stamps_offsets(self):
        agent, dir_p, session, path = self._setup()
        records = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "user", "message": {"content": "there"}},
        ]
        content = "".join(json.dumps(r) + "\n" for r in records)
        path.write_text(content)

        entries = agent.read_logs(dir_p, session)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].source_offset, 0)
        # Second line starts after first line + its newline.
        expected_second = len(json.dumps(records[0]) + "\n")
        self.assertEqual(entries[1].source_offset, expected_second)

    def test_streaming_read_offsets_match_full_read(self):
        agent, dir_p, session, path = self._setup()
        # Seed with one line, read full, then append and stream.
        with path.open("a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "a"}}) + "\n")
        _, cursor = agent.read_logs_since(dir_p, session, None)
        with path.open("a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "b"}}) + "\n")
        new_entries, _ = agent.read_logs_since(dir_p, session, cursor)
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0].source_offset, cursor)


class TestCodexSourceOffset(unittest.TestCase):
    def test_streaming_read_offsets(self):
        tmp = tempfile.mkdtemp()
        rollout = Path(tmp) / "rollout.jsonl"
        rollout.touch()
        agent = CodexAgent()
        first = {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "hello"},
        }
        second = {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "world"},
        }
        with rollout.open("a") as f:
            f.write(json.dumps(first) + "\n")
            f.write(json.dumps(second) + "\n")
        with patch.object(CodexAgent, "_find_rollout_path", return_value=rollout):
            entries, _ = agent.read_logs_since(Path(tmp), "s", None)
        self.assertEqual([e.text for e in entries], ["hello", "world"])
        self.assertEqual(entries[0].source_offset, 0)
        self.assertEqual(entries[1].source_offset, len(json.dumps(first) + "\n"))


# -- session_file_size --------------------------------------------------------

class TestSessionFileSize(unittest.TestCase):
    def test_claude_returns_size_when_file_exists(self):
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        agent = ClaudeAgent()
        dir_p = Path(home) / "work"
        dir_p.mkdir()
        session = "sid"
        path = agent._session_file_path(dir_p, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"abcdef")
        self.assertEqual(agent.session_file_size(dir_p, session), 6)

    def test_claude_returns_none_when_missing(self):
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        agent = ClaudeAgent()
        dir_p = Path(home) / "work"
        dir_p.mkdir()
        self.assertIsNone(agent.session_file_size(dir_p, "no-such"))


# -- Run boundaries in cmd_run -----------------------------------------------

class TestCmdRunCapturesOffsets(unittest.TestCase):
    """cmd_run should snap start_offset before spawn and stamp
    end_offset on finalize. Uses in-memory DB + a fake agent that
    simulates file growth between start() and wait()."""

    def test_start_and_end_offsets_captured(self):
        """On a resume (actor already has a session), cmd_run stats
        the session file before spawn to set log_start_offset, then
        again after wait to set log_end_offset. The fresh-start path
        can't be tested the same way — the session file doesn't
        exist pre-spawn — so start_offset is always 0 there; we
        cover that implicitly via integration tests elsewhere."""
        from actor import cmd_new, cmd_run
        from tests.test_actor import FakeGit, FakeProcessManager

        class _SizingAgent:
            AGENT_DEFAULTS: dict = {}
            ACTOR_DEFAULTS: dict = {}

            def __init__(self) -> None:
                self._call_sizes = [100, 250]
                self._calls = 0

            def emit_agent_args(self, defaults):
                return []

            def apply_actor_keys(self, actor_keys, env):
                return dict(env)

            def start(self, dir, prompt, config):
                return 12345, "session-uuid"

            def resume(self, dir, session_id, prompt, config):
                return 12345

            def wait(self, pid):
                return 0, "output"

            def stop(self, pid):
                pass

            def interactive_argv(self, session_id, config):
                return []

            def read_logs(self, dir, session_id):
                return []

            def read_logs_since(self, dir, session_id, cursor=None):
                return [], None

            def session_file_size(self, dir, session_id):
                idx = min(self._calls, len(self._call_sizes) - 1)
                self._calls += 1
                return self._call_sizes[idx]

        with Database.open(":memory:") as db:
            git = FakeGit()
            pm = FakeProcessManager()
            pm.mark_alive(12345)
            agent = _SizingAgent()
            cmd_new(
                db, git,
                name="sizer", dir="/tmp", no_worktree=True, base=None,
                agent_name="claude", cli_overrides=ActorConfig(),
            )
            # Simulate a prior session so cmd_run takes the resume
            # path and actually stats before spawn.
            db.update_actor_session("sizer", "existing-session")
            cmd_run(
                db, agent, pm, name="sizer", prompt="hello",
                cli_overrides=ActorConfig(),
            )
            run = db.latest_run("sizer")
            assert run is not None
            self.assertEqual(run.log_start_offset, 100)
            self.assertEqual(run.log_end_offset, 250)


# -- Bucketing ----------------------------------------------------------------

def _make_run(
    run_id: int,
    start: Optional[int],
    end: Optional[int],
    status: Status = Status.DONE,
) -> Run:
    return Run(
        id=run_id,
        actor_name="alice",
        prompt="p",
        status=status,
        exit_code=0,
        pid=None,
        config=ActorConfig(),
        started_at="2026-01-01T00:00:00Z",
        finished_at=None if end is None else "2026-01-01T00:01:00Z",
        log_start_offset=start,
        log_end_offset=end,
    )


def _entry(offset: Optional[int]) -> LogEntry:
    return LogEntry(kind=LogEntryKind.USER, text="x", source_offset=offset)


class TestComputeRunRanges(unittest.TestCase):
    def test_closed_runs_use_their_own_end(self):
        runs = [_make_run(1, 0, 100), _make_run(2, 100, 250)]
        ranges = compute_run_ranges(runs)
        self.assertEqual([(r.run_id, r.start, r.end) for r in ranges],
                         [(1, 0, 100), (2, 100, 250)])

    def test_open_run_gets_next_run_start(self):
        runs = [
            _make_run(1, 0, None, status=Status.ERROR),  # stale, never stamped
            _make_run(2, 200, 400),
        ]
        ranges = compute_run_ranges(runs)
        # Run 1 derives end from run 2's start (200), not from current_file_size
        self.assertEqual(ranges[0].end, 200)
        self.assertEqual(ranges[1].end, 400)

    def test_open_run_no_next_uses_current_file_size(self):
        runs = [_make_run(1, 500, None, status=Status.RUNNING)]
        ranges = compute_run_ranges(runs, current_file_size=1024)
        self.assertEqual(ranges[0].end, 1024)

    def test_open_run_no_next_no_file_size_stays_open(self):
        runs = [_make_run(1, 500, None, status=Status.RUNNING)]
        ranges = compute_run_ranges(runs, current_file_size=None)
        self.assertIsNone(ranges[0].end)

    def test_pre_feature_rows_skipped(self):
        # Row with log_start_offset=None cannot be correlated.
        runs = [_make_run(1, None, None), _make_run(2, 0, 100)]
        ranges = compute_run_ranges(runs)
        self.assertEqual([r.run_id for r in ranges], [2])


class TestBucketEntriesByRun(unittest.TestCase):
    def test_entries_fall_into_correct_buckets(self):
        runs = [_make_run(1, 0, 100), _make_run(2, 100, 250)]
        entries = [_entry(10), _entry(99), _entry(100), _entry(200)]
        buckets = bucket_entries_by_run(entries, runs)
        self.assertEqual(len(buckets[1]), 2)  # offsets 10, 99
        self.assertEqual(len(buckets[2]), 2)  # offsets 100, 200

    def test_entries_outside_all_ranges_go_to_none(self):
        runs = [_make_run(1, 100, 200)]
        entries = [_entry(50), _entry(250)]
        buckets = bucket_entries_by_run(entries, runs)
        self.assertEqual(len(buckets[None]), 2)

    def test_entries_without_offset_go_to_none(self):
        runs = [_make_run(1, 0, 100)]
        entries = [_entry(None), _entry(50)]
        buckets = bucket_entries_by_run(entries, runs)
        self.assertEqual(len(buckets[None]), 1)
        self.assertEqual(len(buckets[1]), 1)

    def test_open_interactive_swallows_tail(self):
        # One interactive run is open; entries past its start all
        # belong to it.
        runs = [_make_run(1, 500, None, status=Status.RUNNING)]
        entries = [_entry(600), _entry(700), _entry(1_000_000)]
        buckets = bucket_entries_by_run(entries, runs, current_file_size=None)
        self.assertEqual(len(buckets[1]), 3)


if __name__ == "__main__":
    unittest.main()
