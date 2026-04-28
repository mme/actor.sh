"""Watch app's per-actor log cache invalidation.

The watch keeps `(entries, cursor)` per actor name so switching back to
a previously-viewed actor doesn't re-parse the JSONL from byte 0.
That's an optimization until the same name is reused — discard +
re-create with the same name produces a brand-new session_id and a
fresh JSONL file. The cursor is a byte offset into the prior file
and the bucket is the prior transcript; either left untouched and
the new actor's read appends onto the old transcript.

The fix detects the session_id mismatch in `_refresh_logs` (under
the read lock, before the cursor is consulted) and pops both
caches. These tests exercise that detection path directly via the
@work-decorated method's `__wrapped__` attribute so we don't need
a running Textual loop.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from actor.watch.app import ActorWatchApp


def _bare_app() -> ActorWatchApp:
    """Construct an ActorWatchApp without `__init__`. Only the log
    caches are exercised here, so we set up just those fields and
    the lock the worker takes."""
    app = ActorWatchApp.__new__(ActorWatchApp)
    app._log_entries_by_actor = {}
    app._log_cursors = {}
    app._log_session_for_actor = {}
    app._log_lock = threading.Lock()
    return app


def _fake_actor(name: str, agent_session: str) -> MagicMock:
    a = MagicMock()
    a.name = name
    a.agent_session = agent_session
    a.dir = f"/tmp/{name}"
    return a


class LogCacheSessionInvalidationTests(unittest.TestCase):

    def test_session_change_drops_bucket_and_cursor(self):
        """Discard + recreate scenario: old session left state in the
        caches; new actor with the same name has a different
        session_id; the worker sees the mismatch and pops both
        per-name caches before reading from disk."""
        app = _bare_app()
        app._log_entries_by_actor["hello-world"] = ["old entry 1", "old entry 2"]
        app._log_cursors["hello-world"] = 1234
        app._log_session_for_actor["hello-world"] = "old-session-uuid"

        new_actor = _fake_actor("hello-world", agent_session="new-session-uuid")

        # Worker reads the new file from byte 0; we don't care what
        # comes back here, only that the caches got reset.
        with patch(
            "actor.watch.app.read_log_entries_since",
            return_value=([], 0),
        ), patch.object(app, "call_from_thread"):
            ActorWatchApp._refresh_logs.__wrapped__(app, new_actor)

        # Old bucket dropped — _append_logs would re-populate via
        # call_from_thread, which we mocked out, so it must be absent.
        self.assertNotIn("hello-world", app._log_entries_by_actor)
        # Cursor table gets the freshly-returned offset (0 here from
        # the mocked read), keyed against the new session.
        self.assertEqual(app._log_cursors["hello-world"], 0)
        self.assertEqual(
            app._log_session_for_actor["hello-world"], "new-session-uuid",
        )

    def test_same_session_keeps_bucket_and_advances_cursor(self):
        """Steady-state polling on the same actor: caches survive
        across calls, cursor advances by whatever the read returned."""
        app = _bare_app()
        app._log_entries_by_actor["alice"] = ["entry"]
        app._log_cursors["alice"] = 100
        app._log_session_for_actor["alice"] = "stable-session"

        actor = _fake_actor("alice", agent_session="stable-session")

        with patch(
            "actor.watch.app.read_log_entries_since",
            return_value=([], 250),
        ), patch.object(app, "call_from_thread"):
            ActorWatchApp._refresh_logs.__wrapped__(app, actor)

        # Bucket survives — same session, same name, no reason to wipe.
        self.assertEqual(app._log_entries_by_actor["alice"], ["entry"])
        # Cursor advanced from 100 to 250.
        self.assertEqual(app._log_cursors["alice"], 250)
        self.assertEqual(
            app._log_session_for_actor["alice"], "stable-session",
        )

    def test_first_observation_records_session_without_clearing(self):
        """First time we see this actor name — `_log_session_for_actor`
        has no entry. Don't pop anything (there's nothing to pop), just
        record the session_id so subsequent calls have a baseline to
        compare against."""
        app = _bare_app()
        # Caches start empty; no prior session recorded.

        actor = _fake_actor("brand-new", agent_session="first-session")

        with patch(
            "actor.watch.app.read_log_entries_since",
            return_value=([], 42),
        ), patch.object(app, "call_from_thread"):
            ActorWatchApp._refresh_logs.__wrapped__(app, actor)

        self.assertEqual(
            app._log_session_for_actor["brand-new"], "first-session",
        )
        self.assertEqual(app._log_cursors["brand-new"], 42)

    def test_cursor_only_advances_when_read_succeeds(self):
        """If `read_log_entries_since` returns `next_cursor=None`
        (e.g. no newline yet), the cursor table is left untouched —
        same contract as before the session-detection patch."""
        app = _bare_app()
        app._log_session_for_actor["alice"] = "stable"
        app._log_cursors["alice"] = 100

        actor = _fake_actor("alice", agent_session="stable")

        with patch(
            "actor.watch.app.read_log_entries_since",
            return_value=([], None),
        ), patch.object(app, "call_from_thread"):
            ActorWatchApp._refresh_logs.__wrapped__(app, actor)

        # next_cursor is None → cursor table unchanged.
        self.assertEqual(app._log_cursors["alice"], 100)


if __name__ == "__main__":
    unittest.main()
