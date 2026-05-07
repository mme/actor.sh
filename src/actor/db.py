from __future__ import annotations

import json
import os
import sqlite3
from typing import List, Optional, Tuple

from .errors import ActorError, AlreadyExistsError, NotFoundError
from .interfaces import ProcessManager
from .types import (
    Actor,
    ActorConfig,
    AgentKind,
    Run,
    Status,
    _now_iso,
    _sorted_config,
)


def _actor_config_to_json(cfg: ActorConfig) -> str:
    """Serialize an ActorConfig to the on-disk JSON shape.

    Shape: `{"actor_keys": {...}, "agent_args": {...}}` — mirrors the
    dataclass fields 1:1. Both sub-dicts are stored sorted so rows are
    deterministic and diff-friendly when inspected.

    This is a breaking schema change vs. the pre-refactor flat dict. There
    is no migration — the repo is pre-1.0 and the refactor directive
    accepts a hard break of existing `~/.actor/actor.db` files."""
    return json.dumps({
        "actor_keys": _sorted_config(cfg.actor_keys),
        "agent_args": _sorted_config(cfg.agent_args),
    })


def _json_to_actor_config(s: str) -> ActorConfig:
    """Deserialize the JSON column back into an ActorConfig.

    Missing sub-dicts default to empty, so a row stamped as the legacy
    empty dict `"{}"` yields `ActorConfig()` rather than blowing up. Any
    other unexpected shape (e.g. non-dict sub-fields) raises via the
    dataclass constructor."""
    if not s:
        return ActorConfig()
    data = json.loads(s)
    if not isinstance(data, dict):
        raise ActorError(f"config JSON must be a dict, got {type(data).__name__}")
    actor_keys = data.get("actor_keys", {}) or {}
    agent_args = data.get("agent_args", {}) or {}
    return ActorConfig(
        actor_keys=_sorted_config(actor_keys),
        agent_args=_sorted_config(agent_args),
    )


class Database:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @classmethod
    def open(cls, path: str) -> Database:
        if path == ":memory:":
            conn = sqlite3.connect(":memory:", timeout=30.0, check_same_thread=False)
        else:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # `timeout=30` waits up to 30 seconds for the file lock
            # before raising. PRAGMA statements below also need to
            # acquire the lock — without this, concurrent CLI creates
            # racing against a fresh DB can fail with "database is
            # locked" before busy_timeout below kicks in. 30s is
            # deliberately generous for slower CI runners; in normal
            # use the lock is held for milliseconds.
            #
            # `check_same_thread=False` lets us hand the connection
            # off to executor threads via `asyncio.to_thread` from the
            # async service layer. SQLite itself is fine with this in
            # WAL mode (concurrent readers, serialized writers); we
            # already serialise writes through the single asyncio loop.
            conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)

        # Set busy_timeout FIRST so it covers the PRAGMA statements
        # below, including the journal_mode read.
        conn.execute("PRAGMA busy_timeout=30000;")
        # Only set journal_mode=WAL if the DB isn't already in WAL.
        # Switching journal mode requires an exclusive lock, which
        # serialises behind any in-flight writers. Once a DB has been
        # initialised in WAL once, every subsequent open is just
        # reading the current mode — no lock contention. This is the
        # difference between every `actor` invocation contesting for
        # the WAL lock vs. only the first one ever doing so.
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if (mode or "").lower() != "wal":
            # `PRAGMA journal_mode=WAL` does NOT honor `busy_timeout` —
            # it returns SQLITE_BUSY immediately when any other
            # connection is mid-transaction. With concurrent CLI
            # invocations on a fresh DB, this races and one process
            # wins the switch; the loser raises "database is locked"
            # without retrying. Retry manually with a short backoff —
            # the steady state (after first WAL setup ever) sees WAL
            # on the read above and skips this branch entirely.
            import time as _time
            import random as _random
            for _attempt in range(50):  # ~50 × ~30ms ≈ 1.5s upper bound
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    break
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e):
                        raise
                    # Re-check: another connection may have flipped it
                    # under us; if so, we're done.
                    current = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    if (current or "").lower() == "wal":
                        break
                    _time.sleep(0.02 + _random.random() * 0.04)
        conn.execute("PRAGMA foreign_keys=ON;")

        # Schema init is idempotent (CREATE TABLE IF NOT EXISTS) but
        # `executescript` opens an implicit write transaction even when
        # nothing changes — that was contesting locks with concurrent
        # writers under load. Skip the script when the schema is
        # already complete; only run it on a fresh DB.
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN ('actors', 'runs')"
            )
        }
        if {"actors", "runs"} - existing:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS actors (
                    name            TEXT PRIMARY KEY,
                    agent           TEXT NOT NULL DEFAULT 'claude',
                    agent_session   TEXT,
                    dir             TEXT NOT NULL,
                    source_repo     TEXT,
                    base_branch     TEXT,
                    worktree        BOOLEAN NOT NULL DEFAULT FALSE,
                    parent          TEXT,
                    config          TEXT NOT NULL DEFAULT '{}',
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_name      TEXT NOT NULL REFERENCES actors(name) ON DELETE CASCADE,
                    prompt          TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'running',
                    exit_code       INTEGER,
                    pid             INTEGER,
                    config          TEXT NOT NULL DEFAULT '{}',
                    started_at      TEXT NOT NULL,
                    finished_at     TEXT
                );
            """)
            conn.commit()

        # Migrations: PRAGMA table_info is read-only; ALTER only fires
        # when the column is genuinely missing (legacy DBs predating
        # the `parent` column in the schema script above).
        cur = conn.execute("PRAGMA table_info(actors)")
        columns = {row[1] for row in cur.fetchall()}
        if "parent" not in columns:
            conn.execute("ALTER TABLE actors ADD COLUMN parent TEXT")
            conn.commit()

        return cls(conn)

    # -- Actor CRUD --

    def insert_actor(self, actor: Actor) -> None:
        config_json = _actor_config_to_json(actor.config)
        try:
            self._conn.execute(
                """INSERT INTO actors
                   (name, agent, agent_session, dir, source_repo, base_branch,
                    worktree, parent, config, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    actor.name,
                    actor.agent.as_str(),
                    actor.agent_session,
                    actor.dir,
                    actor.source_repo,
                    actor.base_branch,
                    actor.worktree,
                    actor.parent,
                    config_json,
                    actor.created_at,
                    actor.updated_at,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            raise AlreadyExistsError(actor.name)

    def get_actor(self, name: str) -> Actor:
        cur = self._conn.execute(
            """SELECT name, agent, agent_session, dir, source_repo, base_branch,
                      worktree, parent, config, created_at, updated_at
               FROM actors WHERE name = ?""",
            (name,),
        )
        row = cur.fetchone()
        if row is None:
            raise NotFoundError(name)
        return self._row_to_actor(row)

    def actor_exists(self, name: str) -> bool:
        """Return True if an actor row exists for `name`. Used by the
        MCP server to distinguish "stopped" (process killed, row
        still present) from "discarded" (row deleted) when emitting
        channel notifications back to the parent Claude session."""
        cur = self._conn.execute(
            "SELECT 1 FROM actors WHERE name = ? LIMIT 1", (name,),
        )
        return cur.fetchone() is not None

    def list_actors(self) -> List[Actor]:
        cur = self._conn.execute(
            """SELECT name, agent, agent_session, dir, source_repo, base_branch,
                      worktree, parent, config, created_at, updated_at
               FROM actors ORDER BY created_at DESC"""
        )
        return [self._row_to_actor(row) for row in cur.fetchall()]

    def list_children(self, parent_name: str) -> List[Actor]:
        cur = self._conn.execute(
            """SELECT name, agent, agent_session, dir, source_repo, base_branch,
                      worktree, parent, config, created_at, updated_at
               FROM actors WHERE parent = ?""",
            (parent_name,),
        )
        return [self._row_to_actor(row) for row in cur.fetchall()]

    def delete_actor(self, name: str) -> None:
        cur = self._conn.execute("DELETE FROM actors WHERE name = ?", (name,))
        self._conn.commit()
        if cur.rowcount == 0:
            raise NotFoundError(name)

    def touch_actor(self, name: str) -> None:
        """Update the actor's updated_at timestamp."""
        now = _now_iso()
        cur = self._conn.execute(
            "UPDATE actors SET updated_at = ? WHERE name = ?",
            (now, name),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise NotFoundError(name)

    def update_actor_session(self, name: str, session_id: str) -> None:
        now = _now_iso()
        cur = self._conn.execute(
            "UPDATE actors SET agent_session = ?, updated_at = ? WHERE name = ?",
            (session_id, now, name),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise NotFoundError(name)

    def update_actor_config(self, name: str, config: ActorConfig) -> None:
        config_json = _actor_config_to_json(config)
        now = _now_iso()
        cur = self._conn.execute(
            "UPDATE actors SET config = ?, updated_at = ? WHERE name = ?",
            (config_json, now, name),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise NotFoundError(name)

    # -- Run CRUD --

    def insert_run(self, run: Run) -> int:
        config_json = _actor_config_to_json(run.config)
        cur = self._conn.execute(
            """INSERT INTO runs
               (actor_name, prompt, status, exit_code, pid, config, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.actor_name,
                run.prompt,
                run.status.as_str(),
                run.exit_code,
                run.pid,
                config_json,
                run.started_at,
                run.finished_at,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_run_pid(self, run_id: int, pid: int) -> None:
        cur = self._conn.execute(
            "UPDATE runs SET pid = ? WHERE id = ?",
            (pid, run_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ActorError("run not found")

    def list_running_runs_with_pid(self) -> List[Run]:
        """All `runs` rows currently in `running` state with a recorded
        PID. Used by the daemon's startup orphan sweep to detect runs
        whose agent died while the daemon was down."""
        cur = self._conn.execute(
            """SELECT id, actor_name, prompt, status, exit_code, pid, config,
                      started_at, finished_at
               FROM runs WHERE status = 'running' AND pid IS NOT NULL""",
        )
        return [self._row_to_run(row) for row in cur.fetchall()]

    def update_run_status(self, run_id: int, status: Status, exit_code: Optional[int]) -> None:
        now = _now_iso()
        cur = self._conn.execute(
            "UPDATE runs SET status = ?, exit_code = ?, finished_at = ? WHERE id = ?",
            (status.as_str(), exit_code, now, run_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ActorError("run not found")

    def get_run(self, run_id: int) -> Optional[Run]:
        cur = self._conn.execute(
            """SELECT id, actor_name, prompt, status, exit_code, pid, config,
                      started_at, finished_at
               FROM runs WHERE id = ?""",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def latest_run(self, actor_name: str) -> Optional[Run]:
        cur = self._conn.execute(
            """SELECT id, actor_name, prompt, status, exit_code, pid, config,
                      started_at, finished_at
               FROM runs WHERE actor_name = ? ORDER BY id DESC LIMIT 1""",
            (actor_name,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def list_runs(self, actor_name: str, limit: int) -> Tuple[List[Run], int]:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM runs WHERE actor_name = ?",
            (actor_name,),
        )
        total: int = cur.fetchone()[0]

        cur = self._conn.execute(
            """SELECT id, actor_name, prompt, status, exit_code, pid, config,
                      started_at, finished_at
               FROM runs WHERE actor_name = ? ORDER BY id DESC LIMIT ?""",
            (actor_name, limit),
        )
        runs = [self._row_to_run(row) for row in cur.fetchall()]
        return runs, total

    def resolve_actor_status(self, actor_name: str, pm: ProcessManager) -> Status:
        run = self.latest_run(actor_name)
        if run is None:
            return Status.IDLE
        if run.status == Status.RUNNING:
            if run.pid is None:
                # `cmd_run` inserts the RUNNING row BEFORE calling
                # `agent.start()` so the watch sees the run land
                # immediately, then updates the pid once start
                # returns. For Claude that's a few ms; for Codex,
                # `start()` blocks reading the first stdout line
                # (the `thread.started` event) which can take a
                # second or two. A poll landing in that window would
                # otherwise see `pid=None`, conclude "not alive",
                # and flip the row to ERROR — turning every fresh
                # codex actor into a momentary error flash. Treat
                # the missing pid as "not observed yet" instead.
                return Status.RUNNING
            if not pm.is_alive(run.pid):
                self.update_run_status(run.id, Status.ERROR, -1)
                return Status.ERROR
        return run.status

    # -- Helpers --

    @staticmethod
    def _row_to_actor(row: tuple) -> Actor:
        return Actor(
            name=row[0],
            agent=AgentKind.from_str(row[1]),
            agent_session=row[2],
            dir=row[3],
            source_repo=row[4],
            base_branch=row[5],
            worktree=bool(row[6]),
            parent=row[7],
            config=_json_to_actor_config(row[8]),
            created_at=row[9],
            updated_at=row[10],
        )

    @staticmethod
    def _row_to_run(row: tuple) -> Run:
        return Run(
            id=row[0],
            actor_name=row[1],
            prompt=row[2],
            status=Status.from_str(row[3]),
            exit_code=row[4],
            pid=row[5],
            config=_json_to_actor_config(row[6]),
            started_at=row[7],
            finished_at=row[8],
        )
