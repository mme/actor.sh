"""Cursor-based streaming log reads.

Exercises Agent.read_logs_since (the default full-read fallback plus
the ClaudeAgent / CodexAgent overrides) and the shared byte-level
splitter that keeps partial final lines out of the parse until more
bytes arrive."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actor.agents._jsonl import split_complete_lines
from actor.agents.claude import ClaudeAgent
from actor.agents.codex import CodexAgent
from actor.interfaces import LogEntryKind


def _arun(coro):
    """Drive an async agent coroutine from a sync test method."""
    return asyncio.run(coro)


class TestSplitCompleteLines(unittest.TestCase):
    """The shared byte-level helper. Keeps the partial-line contract
    consistent across agents."""

    def test_no_newline_returns_none_advance(self):
        text, advance = split_complete_lines(b"partial line")
        self.assertEqual(text, "")
        self.assertIsNone(advance)

    def test_single_complete_line(self):
        text, advance = split_complete_lines(b"line one\n")
        self.assertEqual(text, "line one\n")
        self.assertEqual(advance, len(b"line one\n"))

    def test_complete_then_partial(self):
        text, advance = split_complete_lines(b"one\ntwo\npart")
        self.assertEqual(text, "one\ntwo\n")
        self.assertEqual(advance, len(b"one\ntwo\n"))

    def test_multiple_complete_lines_all_terminated(self):
        text, advance = split_complete_lines(b"a\nb\nc\n")
        self.assertEqual(text, "a\nb\nc\n")
        self.assertEqual(advance, len(b"a\nb\nc\n"))

    def test_empty_input_returns_empty(self):
        text, advance = split_complete_lines(b"")
        self.assertEqual(text, "")
        self.assertIsNone(advance)

    def test_utf8_is_preserved_verbatim(self):
        # 4-byte UTF-8 characters crossing read boundaries stay whole
        # because we only slice at ASCII newlines.
        text, advance = split_complete_lines("héllo 🌻\nnext\n".encode("utf-8"))
        self.assertIn("héllo 🌻", text)
        self.assertIn("next", text)


class TestClaudeStreamingReads(unittest.TestCase):
    """ClaudeAgent.read_logs_since — byte-offset cursor into the
    session JSONL, re-parsing only the tail that has arrived."""

    def _with_session(self) -> tuple[ClaudeAgent, Path, str]:
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        agent = ClaudeAgent()
        dir_path = Path(home) / "work"
        dir_path.mkdir()
        session_id = "abc-session"
        session_path = agent._session_file_path(dir_path, session_id)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.touch()
        return agent, dir_path, session_id

    def _append(self, agent: ClaudeAgent, dir_path: Path, session_id: str, *records) -> None:
        path = agent._session_file_path(dir_path, session_id)
        with path.open("a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _user(self, text: str) -> dict:
        return {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": text},
        }

    def test_full_read_when_cursor_none(self):
        agent, dir_path, session_id = self._with_session()
        self._append(agent, dir_path, session_id, self._user("hello"), self._user("world"))
        entries, cursor = _arun(agent.read_logs_since(dir_path, session_id, None))
        self.assertEqual([e.text for e in entries], ["hello", "world"])
        self.assertIsInstance(cursor, int)
        self.assertGreater(cursor, 0)

    def test_cursor_resumes_after_previous_read(self):
        agent, dir_path, session_id = self._with_session()
        self._append(agent, dir_path, session_id, self._user("first"))
        _, cursor = _arun(agent.read_logs_since(dir_path, session_id, None))
        self._append(agent, dir_path, session_id, self._user("second"))
        entries, cursor2 = _arun(agent.read_logs_since(dir_path, session_id, cursor))
        self.assertEqual([e.text for e in entries], ["second"])
        self.assertGreater(cursor2, cursor)

    def test_no_new_content_returns_empty_with_same_cursor(self):
        agent, dir_path, session_id = self._with_session()
        self._append(agent, dir_path, session_id, self._user("only"))
        _, cursor = _arun(agent.read_logs_since(dir_path, session_id, None))
        entries, cursor2 = _arun(agent.read_logs_since(dir_path, session_id, cursor))
        self.assertEqual(entries, [])
        self.assertEqual(cursor2, cursor)

    def test_partial_final_line_deferred(self):
        agent, dir_path, session_id = self._with_session()
        path = agent._session_file_path(dir_path, session_id)
        # Write a complete line then a partial follow-up.
        with path.open("a") as f:
            f.write(json.dumps(self._user("first")) + "\n")
            f.write('{"type":"user","message":{"content":"part')  # no newline
        entries, cursor = _arun(agent.read_logs_since(dir_path, session_id, None))
        # Only the first, fully-terminated line is parsed.
        self.assertEqual([e.text for e in entries], ["first"])
        # Cursor advances only past the complete line, not the partial.
        self.assertEqual(cursor, path.stat().st_size - len('{"type":"user","message":{"content":"part'))
        # When the rest of the partial line arrives, next read picks it up.
        with path.open("a") as f:
            f.write('ial"}}\n')
        entries, cursor2 = _arun(agent.read_logs_since(dir_path, session_id, cursor))
        self.assertEqual([e.text for e in entries], ["partial"])
        self.assertEqual(cursor2, path.stat().st_size)

    def test_stale_cursor_beyond_file_size_resets_to_zero(self):
        # File rotation / truncation: the stored cursor is now past EOF.
        agent, dir_path, session_id = self._with_session()
        self._append(agent, dir_path, session_id, self._user("fresh"))
        entries, cursor = _arun(
            agent.read_logs_since(dir_path, session_id, 10_000_000),
        )
        # A stale cursor triggers a full re-read.
        self.assertEqual([e.text for e in entries], ["fresh"])
        self.assertIsInstance(cursor, int)

    def test_missing_file_returns_empty_preserves_cursor(self):
        agent = ClaudeAgent()
        dir_path = Path(tempfile.mkdtemp())
        entries, cursor = _arun(agent.read_logs_since(dir_path, "no-such-session", 42))
        self.assertEqual(entries, [])
        self.assertEqual(cursor, 42)


class TestCodexStreamingReads(unittest.TestCase):
    """CodexAgent.read_logs_since — byte-offset cursor into the
    rollout file. Codex uses a SQLite-mapped rollout path; tests
    stub the path lookup to avoid the SQLite dance."""

    def _with_rollout(self) -> tuple[CodexAgent, Path, str, Path]:
        tmpdir = tempfile.mkdtemp()
        rollout = Path(tmpdir) / "rollout.jsonl"
        rollout.touch()
        agent = CodexAgent()
        return agent, Path(tmpdir), "session-id", rollout

    def _append(self, rollout: Path, *records) -> None:
        with rollout.open("a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _agent_message(self, text: str) -> dict:
        return {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": text},
        }

    def test_streaming_read_returns_only_tail(self):
        agent, dir_path, session_id, rollout = self._with_rollout()
        self._append(rollout, self._agent_message("hello"))
        with patch.object(CodexAgent, "_find_rollout_path", return_value=rollout):
            _, cursor = _arun(agent.read_logs_since(dir_path, session_id, None))
            self._append(rollout, self._agent_message("world"))
            entries, cursor2 = _arun(agent.read_logs_since(dir_path, session_id, cursor))
        self.assertEqual([e.text for e in entries], ["world"])
        self.assertGreater(cursor2, cursor)

    def test_missing_rollout_preserves_cursor(self):
        agent = CodexAgent()
        with patch.object(CodexAgent, "_find_rollout_path", return_value=None):
            entries, cursor = _arun(agent.read_logs_since(
                Path("/tmp"), "unknown", cursor=7,
            ))
        self.assertEqual(entries, [])
        self.assertEqual(cursor, 7)


class TestDefaultAgentReadLogsSince(unittest.TestCase):
    """Agents that don't override get a safe full-read fallback."""

    def test_default_falls_back_to_full_read(self):
        from actor.interfaces import Agent

        class FullReadAgent(Agent):
            AGENT_DEFAULTS = {}
            ACTOR_DEFAULTS = {}
            binary_name = "fake"

            def emit_agent_args(self, defaults): return []
            def apply_actor_keys(self, flat, env): return dict(env)
            async def start(self, dir, prompt, config): return 0, None
            async def resume(self, dir, session_id, prompt, config): return 0
            async def wait(self, pid): return 0, ""
            async def read_logs(self, dir, session_id): return ["FULL-READ-MARKER"]
            async def stop(self, pid): pass
            def interactive_argv(self, session_id, config): return []

        agent = FullReadAgent()
        entries, cursor = _arun(agent.read_logs_since(Path("/"), "s", cursor=999))
        self.assertEqual(entries, ["FULL-READ-MARKER"])
        self.assertIsNone(cursor)


if __name__ == "__main__":
    unittest.main()
