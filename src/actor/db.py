from __future__ import annotations

import json
import os
import sqlite3
from typing import List, Optional, Tuple

from .errors import ActorError, AlreadyExistsError, NotFoundError
from .interfaces import ProcessManager
from .types import (
    Actor,
    AgentKind,
    Config,
    Run,
    Status,
    _now_iso,
    _sorted_config,
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
            conn = sqlite3.connect(":memory:")
        else:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(path)

        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")

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

        # Migrations
        cur = conn.execute("PRAGMA table_info(actors)")
        columns = {row[1] for row in cur.fetchall()}
        if "parent" not in columns:
            conn.execute("ALTER TABLE actors ADD COLUMN parent TEXT")
            conn.commit()

        return cls(conn)

    # -- Actor CRUD --

    def insert_actor(self, actor: Actor) -> None:
        config_json = json.dumps(_sorted_config(actor.config))
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

    def update_actor_config(self, name: str, config: Config) -> None:
        config_json = json.dumps(_sorted_config(config))
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
        config_json = json.dumps(_sorted_config(run.config))
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
            alive = run.pid is not None and pm.is_alive(run.pid)
            if not alive:
                self.update_run_status(run.id, Status.ERROR, -1)
                return Status.ERROR
        return run.status

    # -- Helpers --

    @staticmethod
    def _row_to_actor(row: tuple) -> Actor:
        config: Config = json.loads(row[8]) if row[8] else {}
        return Actor(
            name=row[0],
            agent=AgentKind.from_str(row[1]),
            agent_session=row[2],
            dir=row[3],
            source_repo=row[4],
            base_branch=row[5],
            worktree=bool(row[6]),
            parent=row[7],
            config=_sorted_config(config),
            created_at=row[9],
            updated_at=row[10],
        )

    @staticmethod
    def _row_to_run(row: tuple) -> Run:
        config: Config = json.loads(row[6]) if row[6] else {}
        return Run(
            id=row[0],
            actor_name=row[1],
            prompt=row[2],
            status=Status.from_str(row[3]),
            exit_code=row[4],
            pid=row[5],
            config=_sorted_config(config),
            started_at=row[7],
            finished_at=row[8],
        )
