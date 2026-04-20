#!/usr/bin/env python3
"""Tests for actor CLI — ported from the Rust test suite (153 tests)."""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path

from actor import (
    # Exceptions
    ActorError, AlreadyExistsError, NotFoundError, IsRunningError, NotRunningError,
    InvalidNameError, AgentNotFoundError, GitError, ConfigError,
    # Types
    AgentKind, Status, Actor, Run, LogEntry, LogEntryKind,
    validate_name, parse_config,
    # ABCs
    Agent, GitOps, ProcessManager,
    # Database
    Database,
    # Commands
    cmd_new, cmd_run, cmd_list, cmd_show, cmd_stop, cmd_config, cmd_logs, cmd_discard,
    cmd_interactive, INTERACTIVE_PROMPT,
    # Helpers
    truncate, format_duration,
)

# Aliases for brevity (tests use short names)
AlreadyExists = AlreadyExistsError
NotFound = NotFoundError
IsRunning = IsRunningError
NotRunning = NotRunningError
InvalidName = InvalidNameError
AgentNotFound = AgentNotFoundError


# ──────────────────────────────────────────────────────────────────────
#  Fakes (test doubles)
# ──────────────────────────────────────────────────────────────────────

class FakeAgentCall:
    def __init__(self, dir: str, prompt: str, config: dict, session_id: str | None):
        self.dir = dir
        self.prompt = prompt
        self.config = config
        self.session_id = session_id


class FakeAgent(Agent):
    def __init__(self):
        self.next_pid: int = 1000
        self.next_session_id: str | None = "fake-session-id"
        self.next_exit_code: int = 0
        self.calls: list[FakeAgentCall] = []
        self.logs: list[LogEntry] = []
        self.next_start_error: ActorError | None = None
        self.next_stop_error: ActorError | None = None
        self.stops: list[int] = []
        self.log_calls: list[tuple[str, str]] = []

    def set_exit_code(self, code: int):
        self.next_exit_code = code

    def set_session_id(self, sid: str | None):
        self.next_session_id = sid

    def set_start_error(self, err: ActorError):
        self.next_start_error = err

    def set_stop_error(self, err: ActorError):
        self.next_stop_error = err

    # -- Agent interface --

    def start(self, dir: str, prompt: str, config: dict) -> tuple[int, str | None]:
        if self.next_start_error is not None:
            err = self.next_start_error
            self.next_start_error = None
            raise err
        pid = self.next_pid
        self.next_pid += 1
        session_id = self.next_session_id
        self.calls.append(FakeAgentCall(dir, prompt, dict(config), session_id=None))
        return pid, session_id

    def resume(self, dir: str, session_id: str, prompt: str, config: dict) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self.calls.append(FakeAgentCall(dir, prompt, dict(config), session_id=session_id))
        return pid

    def wait(self, pid: int) -> tuple[int, str]:
        return (self.next_exit_code, "fake output")

    def read_logs(self, dir: str, session_id: str) -> list[LogEntry]:
        self.log_calls.append((dir, session_id))
        return list(self.logs)

    def stop(self, pid: int):
        self.stops.append(pid)
        if self.next_stop_error is not None:
            err = self.next_stop_error
            self.next_stop_error = None
            raise err

    def interactive_argv(self, session_id, config):
        return ["fake-agent", "--resume", session_id]


class FakeGitCall:
    def __init__(self, op: str, **kwargs):
        self.op = op
        self.__dict__.update(kwargs)


class FakeGit(GitOps):
    def __init__(self):
        self.calls: list[FakeGitCall] = []
        self.current_branch_name: str = "main"
        self.is_repo_value: bool = True
        self.fail_next: str | None = None

    def _check_fail(self, op: str):
        if self.fail_next == op:
            self.fail_next = None
            raise GitError(f"fake {op} failure")

    def create_worktree(self, repo: str, target: str, branch: str, base: str):
        self._check_fail("create_worktree")
        self.calls.append(FakeGitCall("create_worktree", repo=repo, target=target, branch=branch, base=base))

    def remove_worktree(self, repo: str, target: str):
        self._check_fail("remove_worktree")
        self.calls.append(FakeGitCall("remove_worktree", repo=repo, target=target))

    def merge_branch(self, repo: str, branch: str, into: str):
        self._check_fail("merge_branch")
        self.calls.append(FakeGitCall("merge_branch", repo=repo, branch=branch, into=into))

    def delete_branch(self, repo: str, branch: str):
        self._check_fail("delete_branch")
        self.calls.append(FakeGitCall("delete_branch", repo=repo, branch=branch))

    def push_branch(self, repo: str, branch: str):
        self._check_fail("push_branch")
        self.calls.append(FakeGitCall("push_branch", repo=repo, branch=branch))

    def create_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> str:
        self._check_fail("create_pr")
        self.calls.append(FakeGitCall("create_pr", repo=repo, branch=branch, base=base, title=title, body=body))
        return "https://github.com/test/test/pull/1"

    def current_branch(self, repo: str) -> str:
        return self.current_branch_name

    def is_repo(self, path: str) -> bool:
        return self.is_repo_value


class FakeProcessManager(ProcessManager):
    def __init__(self):
        self.alive_pids: set[int] = set()

    def mark_alive(self, pid: int):
        self.alive_pids.add(pid)

    def mark_dead(self, pid: int):
        self.alive_pids.discard(pid)

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def kill(self, pid: int):
        self.alive_pids.discard(pid)


# ──────────────────────────────────────────────────────────────────────
#  Shared helpers
# ──────────────────────────────────────────────────────────────────────

def make_actor(name: str, **overrides) -> Actor:
    kwargs = dict(
        name=name,
        agent=AgentKind.CLAUDE,
        agent_session=None,
        dir="/tmp/test",
        source_repo=None,
        base_branch=None,
        worktree=False,
        parent=None,
        config={},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return Actor(**kwargs)


def make_run(actor_name: str, prompt: str, status: Status,
             pid: int | None = None,
             started_at: str = "2026-01-01T00:00:00Z",
             finished_at: str | None = None) -> Run:
    if status == Status.DONE:
        exit_code = 0
    elif status == Status.ERROR:
        exit_code = 1
    else:
        exit_code = None
    if finished_at is None and status != Status.RUNNING:
        finished_at = "2026-01-01T00:01:00Z"
    return Run(
        id=0,
        actor_name=actor_name,
        prompt=prompt,
        status=status,
        exit_code=exit_code,
        pid=pid,
        config={},
        started_at=started_at,
        finished_at=finished_at,
    )


def create_actor(db: Database, name: str, config: list[str] | None = None):
    """Create an actor using cmd_new with FakeGit (no worktree)."""
    git = FakeGit()
    cmd_new(
        db, git,
        name=name,
        dir="/tmp",
        no_worktree=True,
        base=None,
        agent_name="claude",
        config_pairs=config or ["model=sonnet"],
    )


def create_actor_with_worktree(db: Database, git: FakeGit, name: str):
    git.current_branch_name = "main"
    cmd_new(
        db, git,
        name=name,
        dir="/tmp",
        no_worktree=False,
        base=None,
        agent_name="claude",
        config_pairs=[],
    )


# ──────────────────────────────────────────────────────────────────────
#  Test: validate_name  (2 tests from types.rs)
# ──────────────────────────────────────────────────────────────────────

class TestValidateName(unittest.TestCase):

    def test_valid_names(self):
        # Should not raise
        validate_name("foo")
        validate_name("my-actor")
        validate_name("fix_nav.2")
        validate_name("A123")

    def test_invalid_names(self):
        with self.assertRaises(InvalidName):
            validate_name("")
        with self.assertRaises(InvalidName):
            validate_name("-starts-with-dash")
        with self.assertRaises(InvalidName):
            validate_name(".starts-with-dot")
        with self.assertRaises(InvalidName):
            validate_name("has spaces")
        with self.assertRaises(InvalidName):
            validate_name("has/slash")
        with self.assertRaises(InvalidName):
            validate_name("main")
        with self.assertRaises(InvalidName):
            validate_name("master")
        with self.assertRaises(InvalidName):
            validate_name("HEAD")
        with self.assertRaises(InvalidName):
            validate_name("a" * 65)


# ──────────────────────────────────────────────────────────────────────
#  Test: parse_config  (3 tests from new.rs)
# ──────────────────────────────────────────────────────────────────────

class TestParseConfig(unittest.TestCase):

    def test_parse_config_valid(self):
        config = parse_config(["model=sonnet", "max-budget-usd=5"])
        self.assertEqual(config["model"], "sonnet")
        self.assertEqual(config["max-budget-usd"], "5")

    def test_parse_config_value_with_equals(self):
        config = parse_config(["prompt=a=b=c"])
        self.assertEqual(config["prompt"], "a=b=c")

    def test_parse_config_bare_key(self):
        config = parse_config(["verbose"])
        self.assertEqual(config["verbose"], "")


# ──────────────────────────────────────────────────────────────────────
#  Test: Database  (14 tests from db.rs)
# ──────────────────────────────────────────────────────────────────────

class TestDatabase(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def test_insert_and_get_actor(self):
        db = self._db()
        actor = make_actor("test-actor")
        db.insert_actor(actor)
        fetched = db.get_actor("test-actor")
        self.assertEqual(fetched.name, "test-actor")
        self.assertEqual(fetched.agent, AgentKind.CLAUDE)

    def test_duplicate_actor_errors(self):
        db = self._db()
        actor = make_actor("dupe")
        db.insert_actor(actor)
        with self.assertRaises(AlreadyExists):
            db.insert_actor(make_actor("dupe"))

    def test_get_missing_actor_errors(self):
        db = self._db()
        with self.assertRaises(NotFound):
            db.get_actor("nope")

    def test_list_actors_empty(self):
        db = self._db()
        actors = db.list_actors()
        self.assertEqual(len(actors), 0)

    def test_insert_and_get_run(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        run = Run(
            id=0,
            actor_name="test",
            prompt="do the thing",
            status=Status.RUNNING,
            exit_code=None,
            pid=1234,
            config={},
            started_at="2026-01-01T00:00:00Z",
            finished_at=None,
        )
        run_id = db.insert_run(run)
        latest = db.latest_run("test")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.id, run_id)
        self.assertEqual(latest.prompt, "do the thing")
        self.assertEqual(latest.status, Status.RUNNING)

    def test_actor_status_idle_when_no_runs(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        latest = db.latest_run("test")
        self.assertIsNone(latest)

    def test_actor_status_from_latest_run(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        run1 = Run(
            id=0, actor_name="test", prompt="first", status=Status.DONE,
            exit_code=0, pid=1, config={},
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )
        db.insert_run(run1)
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.DONE)

        run2 = Run(
            id=0, actor_name="test", prompt="second", status=Status.ERROR,
            exit_code=1, pid=2, config={},
            started_at="2026-01-01T00:02:00Z",
            finished_at="2026-01-01T00:03:00Z",
        )
        db.insert_run(run2)
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.ERROR)

    def test_update_run_status(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        run = Run(
            id=0, actor_name="test", prompt="go", status=Status.RUNNING,
            exit_code=None, pid=99, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        run_id = db.insert_run(run)
        db.update_run_status(run_id, Status.DONE, exit_code=0)
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.DONE)
        self.assertEqual(latest.exit_code, 0)
        self.assertIsNotNone(latest.finished_at)

    def test_delete_actor_cascades_runs(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        run = Run(
            id=0, actor_name="test", prompt="go", status=Status.DONE,
            exit_code=0, pid=1, config={},
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )
        db.insert_run(run)
        db.delete_actor("test")
        self.assertIsNone(db.latest_run("test"))

    def test_list_runs_with_limit(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        for i in range(10):
            r = Run(
                id=0, actor_name="test", prompt=f"prompt {i}",
                status=Status.DONE, exit_code=0, pid=i, config={},
                started_at=f"2026-01-01T00:{i:02d}:00Z",
                finished_at=f"2026-01-01T00:{i:02d}:30Z",
            )
            db.insert_run(r)
        runs, total = db.list_runs("test", limit=5)
        self.assertEqual(len(runs), 5)
        self.assertEqual(total, 10)
        # Most recent first
        self.assertEqual(runs[0].prompt, "prompt 9")

    def test_update_actor_session_missing_actor_errors(self):
        db = self._db()
        with self.assertRaises(NotFound):
            db.update_actor_session("nonexistent", "sess-123")

    def test_update_actor_config_missing_actor_errors(self):
        db = self._db()
        with self.assertRaises(NotFound):
            db.update_actor_config("nonexistent", {})

    def test_update_run_status_missing_run_errors(self):
        db = self._db()
        with self.assertRaises(ActorError):
            db.update_run_status(99999, Status.DONE, exit_code=0)

    def test_config_round_trips(self):
        db = self._db()
        actor = make_actor("test", config={"model": "sonnet", "max-budget-usd": "5"})
        db.insert_actor(actor)
        fetched = db.get_actor("test")
        self.assertEqual(fetched.config["model"], "sonnet")
        self.assertEqual(fetched.config["max-budget-usd"], "5")


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_new  (7 tests from new.rs — parse_config tests above)
# ──────────────────────────────────────────────────────────────────────

class TestCmdNew(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def test_new_actor_no_worktree(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="test-actor",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name="claude",
            config_pairs=["model=sonnet"],
        )
        self.assertEqual(actor.name, "test-actor")
        self.assertFalse(actor.worktree)
        self.assertIsNone(actor.source_repo)
        self.assertIsNone(actor.base_branch)
        self.assertEqual(actor.config["model"], "sonnet")
        fetched = db.get_actor("test-actor")
        self.assertEqual(fetched.name, "test-actor")

    def test_new_actor_with_worktree(self):
        db = self._db()
        git = FakeGit()
        git.current_branch_name = "develop"
        actor = cmd_new(
            db, git,
            name="my-feature",
            dir="/tmp",
            no_worktree=False,
            base=None,
            agent_name="claude",
            config_pairs=[],
        )
        self.assertTrue(actor.worktree)
        self.assertEqual(actor.base_branch, "develop")
        self.assertIsNotNone(actor.source_repo)
        self.assertEqual(len(git.calls), 1)

    def test_new_actor_with_explicit_base(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="my-feature",
            dir="/tmp",
            no_worktree=False,
            base="release-1.0",
            agent_name="claude",
            config_pairs=[],
        )
        self.assertEqual(actor.base_branch, "release-1.0")

    def test_new_actor_invalid_name(self):
        db = self._db()
        git = FakeGit()
        with self.assertRaises(InvalidName):
            cmd_new(
                db, git,
                name="main",
                dir="/tmp",
                no_worktree=True,
                base=None,
                agent_name="claude",
                config_pairs=[],
            )

    def test_new_actor_duplicate_name(self):
        db = self._db()
        git = FakeGit()
        kwargs = dict(
            name="dupe", dir="/tmp", no_worktree=True,
            base=None, agent_name="claude", config_pairs=[],
        )
        cmd_new(db, git, **kwargs)
        with self.assertRaises(AlreadyExists):
            cmd_new(db, git, **kwargs)

    def test_new_actor_not_a_repo_falls_back_to_no_worktree(self):
        db = self._db()
        git = FakeGit()
        git.is_repo_value = False
        actor = cmd_new(
            db, git,
            name="feature",
            dir="/tmp",
            no_worktree=False,
            base=None,
            agent_name="claude",
            config_pairs=[],
        )
        self.assertFalse(actor.worktree)
        self.assertIsNone(actor.source_repo)
        self.assertEqual(len(git.calls), 0)

    def test_new_actor_worktree_failure_no_db_row(self):
        db = self._db()
        git = FakeGit()
        git.fail_next = "create_worktree"
        with self.assertRaises(GitError):
            cmd_new(
                db, git,
                name="feature",
                dir="/tmp",
                no_worktree=False,
                base=None,
                agent_name="claude",
                config_pairs=[],
            )
        with self.assertRaises(NotFound):
            db.get_actor("feature")


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_new with --template (ticket #29)
# ──────────────────────────────────────────────────────────────────────

class TestCmdNewTemplate(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def _cfg_with_qa(self):
        from actor import AppConfig, Template
        return AppConfig(templates={
            "qa": Template(
                name="qa",
                agent="codex",
                prompt="you're qa",
                config={"model": "opus", "effort": "max"},
            ),
        })

    def test_template_applies_agent_and_config(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="fix-auth",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name=None,
            config_pairs=[],
            template_name="qa",
            app_config=self._cfg_with_qa(),
        )
        self.assertEqual(actor.agent, AgentKind.CODEX)
        self.assertEqual(actor.config["model"], "opus")
        self.assertEqual(actor.config["effort"], "max")

    def test_cli_agent_overrides_template_agent(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="fix-auth",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name="claude",
            config_pairs=[],
            template_name="qa",
            app_config=self._cfg_with_qa(),
        )
        self.assertEqual(actor.agent, AgentKind.CLAUDE)

    def test_cli_config_pair_overrides_template_config(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="fix-auth",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name=None,
            config_pairs=["model=haiku"],
            template_name="qa",
            app_config=self._cfg_with_qa(),
        )
        self.assertEqual(actor.config["model"], "haiku")
        self.assertEqual(actor.config["effort"], "max")

    def test_unknown_template_raises_config_error(self):
        from actor.errors import ConfigError
        db = self._db()
        git = FakeGit()
        with self.assertRaises(ConfigError):
            cmd_new(
                db, git,
                name="fix-auth",
                dir="/tmp",
                no_worktree=True,
                base=None,
                agent_name=None,
                config_pairs=[],
                template_name="does-not-exist",
                app_config=self._cfg_with_qa(),
            )

    def test_no_template_backward_compatible(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="plain",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name="claude",
            config_pairs=["model=sonnet"],
        )
        self.assertEqual(actor.agent, AgentKind.CLAUDE)
        self.assertEqual(actor.config["model"], "sonnet")

    def test_agent_name_none_without_template_defaults_to_claude(self):
        db = self._db()
        git = FakeGit()
        actor = cmd_new(
            db, git,
            name="x",
            dir="/tmp",
            no_worktree=True,
            base=None,
            agent_name=None,
            config_pairs=[],
        )
        self.assertEqual(actor.agent, AgentKind.CLAUDE)


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_run  (8 tests from run.rs)
# ──────────────────────────────────────────────────────────────────────

class TestCmdRun(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def test_run_first_time_starts_session(self):
        db = self._db()
        create_actor(db, "test")
        agent = FakeAgent()
        pm = FakeProcessManager()
        cmd_run(db, agent, pm, name="test", prompt="do the thing", config_pairs=[])
        actor = db.get_actor("test")
        self.assertIsNotNone(actor.agent_session)
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.DONE)
        self.assertEqual(latest.exit_code, 0)
        self.assertEqual(latest.prompt, "do the thing")

    def test_run_resumes_existing_session(self):
        db = self._db()
        create_actor(db, "test")
        agent = FakeAgent()
        pm = FakeProcessManager()
        cmd_run(db, agent, pm, name="test", prompt="first", config_pairs=[])
        cmd_run(db, agent, pm, name="test", prompt="second", config_pairs=[])
        self.assertEqual(len(agent.calls), 2)
        # Second call should have a session_id (resume)
        self.assertIsNotNone(agent.calls[1].session_id)

    def test_run_errors_if_already_running(self):
        db = self._db()
        create_actor(db, "test")
        pm = FakeProcessManager()
        run = Run(
            id=0, actor_name="test", prompt="go", status=Status.RUNNING,
            exit_code=None, pid=999, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        db.insert_run(run)
        pm.mark_alive(999)
        agent = FakeAgent()
        with self.assertRaises(IsRunning):
            cmd_run(db, agent, pm, name="test", prompt="another", config_pairs=[])

    def test_run_detects_stale_pid_and_proceeds(self):
        db = self._db()
        create_actor(db, "test")
        pm = FakeProcessManager()
        run = Run(
            id=0, actor_name="test", prompt="stale", status=Status.RUNNING,
            exit_code=None, pid=888, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        db.insert_run(run)
        # PID 888 is NOT alive
        agent = FakeAgent()
        cmd_run(db, agent, pm, name="test", prompt="retry", config_pairs=[])
        runs, _ = db.list_runs("test", limit=10)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].status, Status.DONE)    # latest
        self.assertEqual(runs[1].status, Status.ERROR)    # stale

    def test_run_with_config_overrides(self):
        db = self._db()
        create_actor(db, "test")  # has model=sonnet
        agent = FakeAgent()
        pm = FakeProcessManager()
        cmd_run(db, agent, pm, name="test", prompt="go",
                config_pairs=["model=opus", "max-budget-usd=10"])
        latest = db.latest_run("test")
        self.assertEqual(latest.config["model"], "opus")        # overridden
        self.assertEqual(latest.config["max-budget-usd"], "10") # added
        # Actor config unchanged
        actor = db.get_actor("test")
        self.assertEqual(actor.config["model"], "sonnet")

    def test_run_with_nonzero_exit_sets_error(self):
        db = self._db()
        create_actor(db, "test")
        agent = FakeAgent()
        agent.set_exit_code(1)
        pm = FakeProcessManager()
        cmd_run(db, agent, pm, name="test", prompt="fail", config_pairs=[])
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.ERROR)
        self.assertEqual(latest.exit_code, 1)

    def test_run_actor_not_found(self):
        db = self._db()
        agent = FakeAgent()
        pm = FakeProcessManager()
        with self.assertRaises(NotFound):
            cmd_run(db, agent, pm, name="nope", prompt="go", config_pairs=[])

    def test_run_agent_start_failure_marks_run_as_error(self):
        db = self._db()
        create_actor(db, "test")
        agent = FakeAgent()
        agent.set_start_error(ActorError("agent crashed"))
        pm = FakeProcessManager()
        with self.assertRaises(ActorError):
            cmd_run(db, agent, pm, name="test", prompt="do the thing", config_pairs=[])
        latest = db.latest_run("test")
        self.assertIsNotNone(latest, "run row should exist marked as error")
        self.assertEqual(latest.status, Status.ERROR)
        self.assertEqual(latest.exit_code, -1)


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_interactive
# ──────────────────────────────────────────────────────────────────────

class TestCmdInteractive(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def _actor_with_session(self, db: Database, name: str = "test", session: str = "S1"):
        create_actor(db, name)
        db.update_actor_session(name, session)

    def test_interactive_creates_run_and_marks_done_on_success(self):
        db = self._db()
        self._actor_with_session(db)
        pm = FakeProcessManager()
        agent = FakeAgent()
        captured: dict = {}

        def runner(argv, cwd, env):
            captured["argv"] = argv
            captured["cwd"] = cwd
            captured["actor_name_env"] = env.get("ACTOR_NAME")
            return 0

        exit_code, _ = cmd_interactive(
            db, agent, pm, name="test", runner=runner,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["argv"], ["fake-agent", "--resume", "S1"])
        self.assertEqual(Path(captured["cwd"]).resolve(), Path("/tmp").resolve())
        self.assertEqual(captured["actor_name_env"], "test")

        latest = db.latest_run("test")
        self.assertEqual(latest.prompt, INTERACTIVE_PROMPT)
        self.assertEqual(latest.status, Status.DONE)
        self.assertEqual(latest.exit_code, 0)

    def test_interactive_nonzero_exit_marks_error(self):
        db = self._db()
        self._actor_with_session(db)
        pm = FakeProcessManager()
        agent = FakeAgent()

        exit_code, _ = cmd_interactive(
            db, agent, pm, name="test",
            runner=lambda argv, cwd, env: 2,
        )
        self.assertEqual(exit_code, 2)
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.ERROR)
        self.assertEqual(latest.exit_code, 2)

    def test_interactive_requires_session(self):
        db = self._db()
        create_actor(db, "test")  # no session
        pm = FakeProcessManager()
        agent = FakeAgent()
        with self.assertRaises(ActorError):
            cmd_interactive(
                db, agent, pm, name="test",
                runner=lambda argv, cwd, env: 0,
            )

    def test_interactive_rejects_running_actor(self):
        db = self._db()
        self._actor_with_session(db)
        pm = FakeProcessManager()
        run = Run(
            id=0, actor_name="test", prompt="go", status=Status.RUNNING,
            exit_code=None, pid=999, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        db.insert_run(run)
        pm.mark_alive(999)

        agent = FakeAgent()
        from actor.errors import IsRunningError
        with self.assertRaises(IsRunningError):
            cmd_interactive(
                db, agent, pm, name="test",
                runner=lambda argv, cwd, env: 0,
            )

    def test_interactive_runner_exception_marks_error(self):
        db = self._db()
        self._actor_with_session(db)
        pm = FakeProcessManager()
        agent = FakeAgent()

        def boom(argv, cwd, env):
            raise RuntimeError("something went wrong")

        with self.assertRaises(RuntimeError):
            cmd_interactive(
                db, agent, pm, name="test", runner=boom,
            )
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.ERROR)
        self.assertEqual(latest.exit_code, -1)

    def test_interactive_stopped_race_preserves_stopped_status(self):
        """If cmd_stop marks the run STOPPED while interactive is running,
        cmd_interactive must not overwrite it with DONE/ERROR on exit."""
        db = self._db()
        self._actor_with_session(db)
        pm = FakeProcessManager()
        agent = FakeAgent()

        def stop_race(argv, cwd, env):
            # Simulate the stop race: during the run, status flips to STOPPED
            latest = db.latest_run("test")
            db.update_run_status(latest.id, Status.STOPPED, -1)
            return 0

        cmd_interactive(
            db, agent, pm, name="test", runner=stop_race,
        )
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.STOPPED)


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_list  (17 tests from list.rs)
# ──────────────────────────────────────────────────────────────────────

class TestCmdList(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def test_list_empty_db(self):
        db = self._db()
        pm = FakeProcessManager()
        output = cmd_list(db, pm, status_filter=None)
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("NAME", lines[0])
        self.assertIn("STATUS", lines[0])
        self.assertIn("PROMPT", lines[0])

    def test_list_actors_with_various_statuses(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("alpha"))
        db.insert_actor(make_actor("beta"))
        db.insert_actor(make_actor("gamma"))
        db.insert_run(make_run("alpha", "build the thing", Status.DONE, pid=100))
        db.insert_run(make_run("beta", "fix the bug", Status.RUNNING, pid=200))
        pm.mark_alive(200)
        output = cmd_list(db, pm, status_filter=None)
        lines = output.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("alpha", output)
        self.assertIn("beta", output)
        self.assertIn("gamma", output)
        self.assertIn("done", output)
        self.assertIn("running", output)
        self.assertIn("idle", output)

    def test_running_actor_with_dead_pid_becomes_error(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("stale"))
        run_id = db.insert_run(make_run("stale", "deploy", Status.RUNNING, pid=999))
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("error", output)
        self.assertNotIn("running", output)
        run = db.latest_run("stale")
        self.assertEqual(run.id, run_id)
        self.assertEqual(run.status, Status.ERROR)

    def test_running_actor_with_alive_pid_stays_running(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("alive-actor"))
        db.insert_run(make_run("alive-actor", "work on it", Status.RUNNING, pid=42))
        pm.mark_alive(42)
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("running", output)

    def test_status_filter_done(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("a"))
        db.insert_actor(make_actor("b"))
        db.insert_run(make_run("a", "task a", Status.DONE, pid=1))
        db.insert_run(make_run("b", "task b", Status.RUNNING, pid=2))
        pm.mark_alive(2)
        output = cmd_list(db, pm, status_filter="done")
        self.assertIn("a", output)
        self.assertNotIn("  b ", output)
        lines = output.splitlines()
        self.assertEqual(len(lines), 2)

    def test_status_filter_idle(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("idle-one"))
        db.insert_actor(make_actor("done-one"))
        db.insert_run(make_run("done-one", "some task", Status.DONE, pid=1))
        output = cmd_list(db, pm, status_filter="idle")
        self.assertIn("idle-one", output)
        self.assertNotIn("done-one", output)

    def test_status_filter_error_catches_dead_pid(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("ghost"))
        db.insert_run(make_run("ghost", "doomed task", Status.RUNNING, pid=404))
        # PID 404 not alive => becomes error
        output = cmd_list(db, pm, status_filter="error")
        self.assertIn("ghost", output)
        self.assertIn("error", output)

    def test_status_filter_invalid_returns_error(self):
        db = self._db()
        pm = FakeProcessManager()
        with self.assertRaises(ActorError):
            cmd_list(db, pm, status_filter="bogus")

    def test_prompt_truncation(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("verbose"))
        long_prompt = "a" * 80
        db.insert_run(make_run("verbose", long_prompt, Status.DONE, pid=1))
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("a" * 40 + "...", output)
        self.assertNotIn("a" * 41, output)

    def test_prompt_short_not_truncated(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("brief"))
        db.insert_run(make_run("brief", "fix typo", Status.DONE, pid=1))
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("fix typo", output)

    def test_no_runs_shows_idle_with_empty_prompt(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("fresh"))
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("fresh", output)
        self.assertIn("idle", output)

    def test_column_alignment(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("short"))
        db.insert_actor(make_actor("a-much-longer-name"))
        db.insert_run(make_run("short", "task 1", Status.DONE, pid=1))
        db.insert_run(make_run("a-much-longer-name", "task 2", Status.RUNNING, pid=2))
        pm.mark_alive(2)
        output = cmd_list(db, pm, status_filter=None)
        lines = output.splitlines()
        status_col = lines[0].index("STATUS")
        for line in lines[1:]:
            self.assertGreater(len(line), status_col, f"line too short: {line!r}")

    def test_running_no_pid_stays_running(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("no-pid"))
        db.insert_run(make_run("no-pid", "mystery", Status.RUNNING, pid=None))
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("error", output)

    def test_multiline_prompt_uses_first_line(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("multi"))
        db.insert_run(make_run("multi", "first line\nsecond line\nthird line", Status.DONE, pid=1))
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("first line", output)
        self.assertNotIn("second line", output)

    def test_multiple_runs_uses_latest(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("multi-run"))
        db.insert_run(make_run("multi-run", "old prompt", Status.DONE, pid=1))
        db.insert_run(make_run("multi-run", "new prompt", Status.ERROR, pid=2))
        output = cmd_list(db, pm, status_filter=None)
        self.assertIn("new prompt", output)
        self.assertIn("error", output)

    def test_truncate_multibyte_chars(self):
        self.assertEqual(truncate("\U0001f389\U0001f38a\U0001f388\U0001f381\U0001f384\U0001f383", 3),
                         "\U0001f389\U0001f38a\U0001f388...")
        self.assertEqual(truncate("h\u00e9llo world test", 5), "h\u00e9llo...")

    def test_status_filter_running_only_shows_truly_alive(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("alive"))
        db.insert_actor(make_actor("dead"))
        db.insert_run(make_run("alive", "living", Status.RUNNING, pid=10))
        db.insert_run(make_run("dead", "dying", Status.RUNNING, pid=20))
        pm.mark_alive(10)
        # PID 20 is dead => becomes error
        output = cmd_list(db, pm, status_filter="running")
        self.assertIn("alive", output)
        self.assertNotIn("dead", output)
        lines = output.splitlines()
        self.assertEqual(len(lines), 2)  # header + 1 actor


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_show  (39 tests from show.rs)
# ──────────────────────────────────────────────────────────────────────

class TestCmdShow(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    # -- Details section tests (8) --

    def test_show_basic_actor_details(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("my-actor"))
        output = cmd_show(db, pm, name="my-actor", runs_limit=5)
        self.assertIn("Name:      my-actor", output)
        self.assertIn("Agent:     claude", output)
        self.assertIn("Status:    idle", output)
        self.assertIn("Dir:       /tmp/test", output)
        self.assertIn("Created:   2026-01-01T00:00:00Z", output)

    def test_show_actor_not_found(self):
        db = self._db()
        pm = FakeProcessManager()
        with self.assertRaises(NotFound):
            cmd_show(db, pm, name="nonexistent", runs_limit=5)

    def test_show_actor_with_base_branch(self):
        db = self._db()
        pm = FakeProcessManager()
        actor = make_actor("feature", base_branch="main")
        db.insert_actor(actor)
        output = cmd_show(db, pm, name="feature", runs_limit=5)
        self.assertIn("Base:      main", output)

    def test_show_actor_without_base_branch_omits_line(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("no-base"))
        output = cmd_show(db, pm, name="no-base", runs_limit=5)
        self.assertNotIn("Base:", output)

    def test_show_actor_with_config(self):
        db = self._db()
        pm = FakeProcessManager()
        actor = make_actor("configured", config={"model": "sonnet", "max-budget-usd": "5"})
        db.insert_actor(actor)
        output = cmd_show(db, pm, name="configured", runs_limit=5)
        # BTreeMap / sorted dict: max-budget-usd before model
        self.assertIn("Config:    max-budget-usd=5, model=sonnet", output)

    def test_show_actor_empty_config_omits_line(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("no-config"))
        output = cmd_show(db, pm, name="no-config", runs_limit=5)
        self.assertNotIn("Config:", output)

    def test_show_actor_with_session(self):
        db = self._db()
        pm = FakeProcessManager()
        actor = make_actor("sessioned", agent_session="sess-abc123")
        db.insert_actor(actor)
        output = cmd_show(db, pm, name="sessioned", runs_limit=5)
        self.assertIn("Session:   sess-abc123", output)

    def test_show_actor_without_session_omits_line(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("no-session"))
        output = cmd_show(db, pm, name="no-session", runs_limit=5)
        self.assertNotIn("Session:", output)

    # -- Status / stale PID tests (3) --

    def test_show_running_actor_with_alive_pid(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("alive"))
        db.insert_run(make_run("alive", "work", Status.RUNNING, pid=42,
                               started_at="2026-01-01T00:00:00Z"))
        pm.mark_alive(42)
        output = cmd_show(db, pm, name="alive", runs_limit=5)
        self.assertIn("Status:    running", output)

    def test_show_running_actor_with_dead_pid_becomes_error(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("stale"))
        run_id = db.insert_run(make_run("stale", "deploy", Status.RUNNING, pid=999,
                                        started_at="2026-01-01T00:00:00Z"))
        output = cmd_show(db, pm, name="stale", runs_limit=5)
        self.assertIn("Status:    error", output)
        self.assertNotIn("Status:    running", output)
        run = db.latest_run("stale")
        self.assertEqual(run.id, run_id)
        self.assertEqual(run.status, Status.ERROR)

    def test_show_running_no_pid_stays_running(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("no-pid"))
        db.insert_run(make_run("no-pid", "mystery", Status.RUNNING, pid=None,
                               started_at="2026-01-01T00:00:00Z"))
        output = cmd_show(db, pm, name="no-pid", runs_limit=5)
        self.assertIn("Status:    error", output)

    # -- Runs table tests (17) --

    def test_show_no_runs_displays_message(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("empty"))
        output = cmd_show(db, pm, name="empty", runs_limit=5)
        self.assertIn("No runs yet.", output)
        self.assertNotIn("RUN", output)

    def test_show_runs_table_headers(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "do stuff", Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:01:30Z"))
        output = cmd_show(db, pm, name="test", runs_limit=5)
        self.assertIn("RUN", output)
        self.assertIn("STATUS", output)
        self.assertIn("PROMPT", output)
        self.assertIn("DURATION", output)
        self.assertIn("EXIT", output)

    def test_show_run_duration_formatting(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "do stuff", Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:02:30Z"))
        output = cmd_show(db, pm, name="test", runs_limit=5)
        self.assertIn("2m 30s", output)

    def test_show_run_duration_seconds_only(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "quick task", Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:00:45Z"))
        output = cmd_show(db, pm, name="test", runs_limit=5)
        self.assertIn("45s", output)
        self.assertNotIn("0m", output)

    def test_show_running_run_has_dash_duration(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "working", Status.RUNNING, pid=42,
                               started_at="2026-01-01T00:00:00Z"))
        pm.mark_alive(42)
        output = cmd_show(db, pm, name="test", runs_limit=5)
        self.assertIn("\u2014", output)

    def test_show_run_exit_code(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "succeeds", Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:01:00Z"))
        output = cmd_show(db, pm, name="test", runs_limit=5)
        lines = output.splitlines()
        run_line = [l for l in lines if "succeeds" in l][0]
        self.assertTrue(run_line.endswith("0"))

    def test_show_run_prompt_truncation(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        long_prompt = "a" * 80
        db.insert_run(make_run("test", long_prompt, Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:01:00Z"))
        output = cmd_show(db, pm, name="test", runs_limit=5)
        self.assertIn("a" * 40 + "...", output)
        self.assertNotIn("a" * 41, output)

    def test_show_runs_limited_with_more_message(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("busy"))
        for i in range(10):
            db.insert_run(make_run("busy", f"prompt {i}", Status.DONE, pid=i,
                                   started_at=f"2026-01-01T00:{i:02d}:00Z",
                                   finished_at=f"2026-01-01T00:{i:02d}:30Z"))
        output = cmd_show(db, pm, name="busy", runs_limit=5)
        table_lines = [l for l in output.splitlines() if "prompt" in l or "RUN" in l]
        self.assertEqual(len(table_lines), 6)  # header + 5
        self.assertIn("prompt 9", output)
        self.assertIn("prompt 5", output)
        self.assertNotIn("prompt 4", output)
        self.assertIn("10 total runs", output)
        self.assertIn("use --runs to show more", output)

    def test_show_runs_all_fit_no_more_message(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("small"))
        for i in range(3):
            db.insert_run(make_run("small", f"task {i}", Status.DONE, pid=i,
                                   started_at=f"2026-01-01T00:{i:02d}:00Z",
                                   finished_at=f"2026-01-01T00:{i:02d}:30Z"))
        output = cmd_show(db, pm, name="small", runs_limit=5)
        self.assertNotIn("use --runs to show more", output)

    def test_show_runs_zero_hides_section(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "do stuff", Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:01:00Z"))
        output = cmd_show(db, pm, name="test", runs_limit=0)
        self.assertIn("Name:      test", output)
        self.assertNotIn("RUN", output)
        self.assertNotIn("PROMPT", output)
        self.assertNotIn("No runs yet.", output)

    def test_show_multiple_runs_order(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("ordered"))
        db.insert_run(make_run("ordered", "first", Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:01:00Z"))
        db.insert_run(make_run("ordered", "second", Status.DONE, pid=2,
                               started_at="2026-01-01T00:02:00Z",
                               finished_at="2026-01-01T00:03:00Z"))
        db.insert_run(make_run("ordered", "third", Status.ERROR, pid=3,
                               started_at="2026-01-01T00:04:00Z",
                               finished_at="2026-01-01T00:05:00Z"))
        output = cmd_show(db, pm, name="ordered", runs_limit=5)
        self.assertIn("Status:    error", output)
        self.assertIn("first", output)
        self.assertIn("second", output)
        self.assertIn("third", output)
        third_pos = output.index("third")
        first_pos = output.index("first")
        self.assertLess(third_pos, first_pos)

    def test_show_full_output_integration(self):
        db = self._db()
        pm = FakeProcessManager()
        actor = make_actor(
            "my-feature",
            agent_session="sess-xyz",
            base_branch="develop",
            config={"model": "opus"},
        )
        db.insert_actor(actor)
        db.insert_run(make_run("my-feature", "implement login", Status.DONE, pid=100,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:03:15Z"))
        db.insert_run(make_run("my-feature", "add tests for login", Status.DONE, pid=101,
                               started_at="2026-01-01T00:05:00Z",
                               finished_at="2026-01-01T00:06:45Z"))
        output = cmd_show(db, pm, name="my-feature", runs_limit=5)
        self.assertIn("Name:      my-feature", output)
        self.assertIn("Agent:     claude", output)
        self.assertIn("Status:    done", output)
        self.assertIn("Dir:       /tmp/test", output)
        self.assertIn("Base:      develop", output)
        self.assertIn("Config:    model=opus", output)
        self.assertIn("Session:   sess-xyz", output)
        self.assertIn("Created:   2026-01-01T00:00:00Z", output)
        self.assertIn("implement login", output)
        self.assertIn("add tests for login", output)
        self.assertIn("3m 15s", output)
        self.assertIn("1m 45s", output)
        self.assertNotIn("use --runs to show more", output)

    def test_show_exact_limit_no_more_message(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("exact"))
        for i in range(5):
            db.insert_run(make_run("exact", f"task {i}", Status.DONE, pid=i,
                                   started_at=f"2026-01-01T00:{i:02d}:00Z",
                                   finished_at=f"2026-01-01T00:{i:02d}:30Z"))
        output = cmd_show(db, pm, name="exact", runs_limit=5)
        self.assertNotIn("use --runs to show more", output)

    def test_show_runs_custom_limit(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("custom"))
        for i in range(10):
            db.insert_run(make_run("custom", f"prompt {i}", Status.DONE, pid=i,
                                   started_at=f"2026-01-01T00:{i:02d}:00Z",
                                   finished_at=f"2026-01-01T00:{i:02d}:30Z"))
        output = cmd_show(db, pm, name="custom", runs_limit=2)
        self.assertIn("prompt 9", output)
        self.assertIn("prompt 8", output)
        self.assertNotIn("prompt 7", output)
        self.assertIn("10 total runs", output)
        self.assertIn("use --runs to show more", output)

    def test_show_codex_agent(self):
        db = self._db()
        pm = FakeProcessManager()
        actor = make_actor("codex-actor", agent=AgentKind.CODEX)
        db.insert_actor(actor)
        output = cmd_show(db, pm, name="codex-actor", runs_limit=5)
        self.assertIn("Agent:     codex", output)

    def test_show_run_with_error_exit_code(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "fail", Status.ERROR, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:00:10Z"))
        output = cmd_show(db, pm, name="test", runs_limit=5)
        lines = output.splitlines()
        run_line = [l for l in lines if "fail" in l][0]
        self.assertTrue(run_line.endswith("1"))

    def test_show_run_multiline_prompt_in_table(self):
        db = self._db()
        pm = FakeProcessManager()
        db.insert_actor(make_actor("test"))
        db.insert_run(make_run("test", "line one\nline two\nline three", Status.DONE, pid=1,
                               started_at="2026-01-01T00:00:00Z",
                               finished_at="2026-01-01T00:01:00Z"))
        output = cmd_show(db, pm, name="test", runs_limit=5)
        self.assertIn("line one", output)
        self.assertNotIn("line two", output)

    # -- format_duration unit tests (5) --

    def test_format_duration_minutes_and_seconds(self):
        d = format_duration("2026-01-01T00:00:00Z", "2026-01-01T00:05:30Z")
        self.assertEqual(d, "5m 30s")

    def test_format_duration_seconds_only(self):
        d = format_duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:42Z")
        self.assertEqual(d, "42s")

    def test_format_duration_zero_seconds(self):
        d = format_duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        self.assertEqual(d, "0s")

    def test_format_duration_no_finished_at(self):
        d = format_duration("2026-01-01T00:00:00Z", None)
        self.assertEqual(d, "\u2014")

    def test_format_duration_invalid_timestamps(self):
        d = format_duration("not-a-date", "also-not-a-date")
        self.assertEqual(d, "\u2014")

    # -- truncate unit tests (6) --

    def test_truncate_short_string(self):
        self.assertEqual(truncate("hello", 10), "hello")

    def test_truncate_exact_length(self):
        self.assertEqual(truncate("hello", 5), "hello")

    def test_truncate_long_string(self):
        self.assertEqual(truncate("hello world", 5), "hello...")

    def test_truncate_multiline(self):
        self.assertEqual(truncate("first\nsecond\nthird", 10), "first")

    def test_truncate_empty_string(self):
        self.assertEqual(truncate("", 10), "")

    def test_truncate_multibyte_chars(self):
        self.assertEqual(truncate("\U0001f389\U0001f38a\U0001f388\U0001f381\U0001f384\U0001f383", 3),
                         "\U0001f389\U0001f38a\U0001f388...")
        self.assertEqual(truncate("h\u00e9llo world test", 5), "h\u00e9llo...")


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_stop  (10 tests from stop.rs)
# ──────────────────────────────────────────────────────────────────────

class TestCmdStop(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def _insert_running_run(self, db: Database, actor_name: str, pid: int) -> int:
        run = Run(
            id=0, actor_name=actor_name, prompt="do stuff",
            status=Status.RUNNING, exit_code=None, pid=pid,
            config={}, started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        return db.insert_run(run)

    def test_stop_running_actor(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        self._insert_running_run(db, "test", 1234)
        pm.mark_alive(1234)
        msg = cmd_stop(db, agent, pm, name="test")
        self.assertEqual(msg, "test stopped")
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.STOPPED)
        self.assertIsNotNone(latest.finished_at)
        self.assertIsNone(latest.exit_code)

    def test_stop_not_running_actor_errors(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        run = Run(
            id=0, actor_name="test", prompt="done", status=Status.DONE,
            exit_code=0, pid=100, config={},
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )
        db.insert_run(run)
        with self.assertRaises(NotRunning):
            cmd_stop(db, agent, pm, name="test")

    def test_stop_actor_with_no_runs_errors(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        with self.assertRaises(NotRunning):
            cmd_stop(db, agent, pm, name="test")

    def test_stop_nonexistent_actor_errors(self):
        db = self._db()
        agent = FakeAgent()
        pm = FakeProcessManager()
        with self.assertRaises(NotFound):
            cmd_stop(db, agent, pm, name="ghost")

    def test_stop_stale_pid_marks_error(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        self._insert_running_run(db, "test", 5678)
        # Do NOT mark_alive
        msg = cmd_stop(db, agent, pm, name="test")
        self.assertEqual(msg, "test was already dead \u2014 marked as error")
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.ERROR)
        self.assertEqual(latest.exit_code, -1)
        self.assertIsNotNone(latest.finished_at)

    def test_stop_running_with_no_pid_marks_error(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        run = Run(
            id=0, actor_name="test", prompt="weird", status=Status.RUNNING,
            exit_code=None, pid=None, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        db.insert_run(run)
        msg = cmd_stop(db, agent, pm, name="test")
        self.assertEqual(msg, "test was already dead \u2014 marked as error")
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.ERROR)
        self.assertEqual(latest.exit_code, -1)

    def test_stop_sets_finished_at(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        self._insert_running_run(db, "test", 42)
        pm.mark_alive(42)
        cmd_stop(db, agent, pm, name="test")
        latest = db.latest_run("test")
        self.assertIsNotNone(latest.finished_at)
        self.assertIn("T", latest.finished_at)

    def test_stop_only_affects_latest_run(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        run1 = Run(
            id=0, actor_name="test", prompt="first", status=Status.DONE,
            exit_code=0, pid=100, config={},
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )
        id1 = db.insert_run(run1)
        self._insert_running_run(db, "test", 200)
        pm.mark_alive(200)
        cmd_stop(db, agent, pm, name="test")
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.STOPPED)
        runs, _ = db.list_runs("test", limit=10)
        first = [r for r in runs if r.id == id1][0]
        self.assertEqual(first.status, Status.DONE)

    def test_stop_verifies_agent_stop_called(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        pm = FakeProcessManager()
        self._insert_running_run(db, "test", 4242)
        pm.mark_alive(4242)
        cmd_stop(db, agent, pm, name="test")
        self.assertEqual(len(agent.stops), 1)
        self.assertEqual(agent.stops[0], 4242)

    def test_stop_agent_failure_propagates(self):
        db = self._db()
        create_actor(db, "test", config=[])
        agent = FakeAgent()
        agent.set_stop_error(ActorError("kill failed"))
        pm = FakeProcessManager()
        self._insert_running_run(db, "test", 5555)
        pm.mark_alive(5555)
        with self.assertRaises(ActorError):
            cmd_stop(db, agent, pm, name="test")
        latest = db.latest_run("test")
        self.assertEqual(latest.status, Status.RUNNING)


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_config  (10 tests from config.rs)
# ──────────────────────────────────────────────────────────────────────

class TestCmdConfig(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def test_display_empty_config(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        result = cmd_config(db, name="test", config_pairs=[])
        self.assertEqual(result, "")

    def test_display_existing_config(self):
        db = self._db()
        actor = make_actor("test", config={"model": "sonnet", "max-turns": "10"})
        db.insert_actor(actor)
        result = cmd_config(db, name="test", config_pairs=[])
        self.assertIn("model=sonnet", result)
        self.assertIn("max-turns=10", result)

    def test_set_config_on_empty(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        cmd_config(db, name="test", config_pairs=["model=sonnet", "max-turns=10"])
        actor = db.get_actor("test")
        self.assertEqual(actor.config["model"], "sonnet")
        self.assertEqual(actor.config["max-turns"], "10")

    def test_merge_config_preserves_existing(self):
        db = self._db()
        actor = make_actor("test", config={"model": "sonnet", "max-turns": "10"})
        db.insert_actor(actor)
        cmd_config(db, name="test", config_pairs=["timeout=30"])
        actor = db.get_actor("test")
        self.assertEqual(actor.config["model"], "sonnet")
        self.assertEqual(actor.config["max-turns"], "10")
        self.assertEqual(actor.config["timeout"], "30")

    def test_merge_config_overwrites_existing_key(self):
        db = self._db()
        actor = make_actor("test", config={"model": "sonnet"})
        db.insert_actor(actor)
        cmd_config(db, name="test", config_pairs=["model=opus"])
        actor = db.get_actor("test")
        self.assertEqual(actor.config["model"], "opus")

    def test_config_value_with_equals_sign(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        cmd_config(db, name="test", config_pairs=["prompt=a=b=c"])
        actor = db.get_actor("test")
        self.assertEqual(actor.config["prompt"], "a=b=c")

    def test_actor_not_found(self):
        db = self._db()
        with self.assertRaises(NotFound):
            cmd_config(db, name="nonexistent", config_pairs=[])

    def test_bare_key_config_pair(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        cmd_config(db, name="test", config_pairs=["verbose"])
        actor = db.get_actor("test")
        self.assertEqual(actor.config["verbose"], "")

    def test_set_multiple_config_pairs_at_once(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        cmd_config(db, name="test", config_pairs=["model=sonnet", "max-turns=10", "timeout=30"])
        actor = db.get_actor("test")
        self.assertEqual(len(actor.config), 3)
        self.assertEqual(actor.config["model"], "sonnet")
        self.assertEqual(actor.config["max-turns"], "10")
        self.assertEqual(actor.config["timeout"], "30")

    def test_successive_config_updates(self):
        db = self._db()
        db.insert_actor(make_actor("test"))
        cmd_config(db, name="test", config_pairs=["model=sonnet"])
        cmd_config(db, name="test", config_pairs=["max-turns=10"])
        cmd_config(db, name="test", config_pairs=["model=opus"])
        actor = db.get_actor("test")
        self.assertEqual(len(actor.config), 2)
        self.assertEqual(actor.config["model"], "opus")
        self.assertEqual(actor.config["max-turns"], "10")


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_logs  (10 tests from logs.rs)
# ──────────────────────────────────────────────────────────────────────

class TestCmdLogs(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    @staticmethod
    def _user(text, ts=None):
        return LogEntry(kind=LogEntryKind.USER, text=text, timestamp=ts)

    @staticmethod
    def _assistant(text, ts=None):
        return LogEntry(kind=LogEntryKind.ASSISTANT, text=text, timestamp=ts)

    @staticmethod
    def _thinking(text, ts=None):
        return LogEntry(kind=LogEntryKind.THINKING, text=text, timestamp=ts)

    @staticmethod
    def _tool_use(name, input_text, ts=None):
        return LogEntry(kind=LogEntryKind.TOOL_USE, name=name, input=input_text, timestamp=ts)

    @staticmethod
    def _tool_result(content, ts=None):
        return LogEntry(kind=LogEntryKind.TOOL_RESULT, content=content, timestamp=ts)

    def test_actor_not_found(self):
        db = self._db()
        agent = FakeAgent()
        with self.assertRaises(NotFound):
            cmd_logs(db, agent, name="nonexistent", watch=False, verbose=False)

    def test_no_session_yet(self):
        db = self._db()
        agent = FakeAgent()
        db.insert_actor(make_actor("my-actor"))
        result = cmd_logs(db, agent, name="my-actor", watch=False, verbose=False)
        self.assertEqual(result, "No session yet \u2014 run the actor first")

    def test_empty_logs(self):
        db = self._db()
        agent = FakeAgent()
        actor = make_actor("my-actor", agent_session="session-123")
        db.insert_actor(actor)
        result = cmd_logs(db, agent, name="my-actor", watch=False, verbose=False)
        self.assertEqual(result, "No log entries found.")

    def test_default_shows_user_and_assistant_only(self):
        db = self._db()
        agent = FakeAgent()
        agent.logs = [
            self._user("fix the bug"),
            self._thinking("Let me look at the code..."),
            self._assistant("I'll fix it now."),
            self._tool_use("Edit", '{"file": "src/main.rs"}'),
            self._tool_result("File updated"),
            self._assistant("Done."),
        ]
        actor = make_actor("test", agent_session="s1")
        db.insert_actor(actor)
        result = cmd_logs(db, agent, name="test", watch=False, verbose=False)
        self.assertEqual(result, "USER: fix the bug\nASSISTANT: I'll fix it now.\nASSISTANT: Done.")

    def test_verbose_shows_everything(self):
        db = self._db()
        agent = FakeAgent()
        agent.logs = [
            self._user("fix the bug"),
            self._thinking("Let me look at the code..."),
            self._assistant("I'll fix it now."),
            self._tool_use("Edit", '{"file": "src/main.rs"}'),
            self._tool_result("File updated"),
            self._assistant("Done."),
        ]
        actor = make_actor("test", agent_session="s1")
        db.insert_actor(actor)
        result = cmd_logs(db, agent, name="test", watch=False, verbose=True)
        lines = result.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[0].endswith("USER: fix the bug"))
        self.assertTrue(lines[1].endswith("THINKING: Let me look at the code..."))
        self.assertTrue(lines[2].endswith("ASSISTANT: I'll fix it now."))
        self.assertIn("TOOL: Edit(", lines[3])
        self.assertIn("RESULT: File updated", lines[4])
        self.assertTrue(lines[5].endswith("ASSISTANT: Done."))

    def test_verbose_shows_timestamps(self):
        db = self._db()
        agent = FakeAgent()
        agent.logs = [
            self._user("hello", ts="2026-04-07T10:30:15+00:00"),
            self._assistant("hi!", ts="2026-04-07T10:30:22+00:00"),
        ]
        actor = make_actor("test", agent_session="s1")
        db.insert_actor(actor)
        result = cmd_logs(db, agent, name="test", watch=False, verbose=True)
        self.assertIn("[2026-04-07 10:30:15] USER: hello", result)
        self.assertIn("[2026-04-07 10:30:22] ASSISTANT: hi!", result)

    def test_default_no_timestamps(self):
        db = self._db()
        agent = FakeAgent()
        agent.logs = [
            self._user("hello", ts="2026-04-07T10:30:15+00:00"),
        ]
        actor = make_actor("test", agent_session="s1")
        db.insert_actor(actor)
        result = cmd_logs(db, agent, name="test", watch=False, verbose=False)
        self.assertEqual(result, "USER: hello")

    def test_multiline_text_preserved(self):
        db = self._db()
        agent = FakeAgent()
        agent.logs = [
            self._assistant("line one\nline two\nline three"),
        ]
        actor = make_actor("test", agent_session="s1")
        db.insert_actor(actor)
        result = cmd_logs(db, agent, name="test", watch=False, verbose=False)
        self.assertEqual(result, "ASSISTANT: line one\nline two\nline three")

    def test_ordering_preserved(self):
        db = self._db()
        agent = FakeAgent()
        agent.logs = [
            self._user("first"),
            self._assistant("second"),
            self._user("third"),
            self._assistant("fourth"),
        ]
        actor = make_actor("test", agent_session="s1")
        db.insert_actor(actor)
        result = cmd_logs(db, agent, name="test", watch=False, verbose=False)
        lines = result.splitlines()
        self.assertEqual(lines, ["USER: first", "ASSISTANT: second", "USER: third", "ASSISTANT: fourth"])

    def test_session_set_via_update(self):
        db = self._db()
        agent = FakeAgent()
        agent.logs = [self._user("ping")]
        db.insert_actor(make_actor("updated"))
        db.update_actor_session("updated", "new-session-id")
        result = cmd_logs(db, agent, name="updated", watch=False, verbose=False)
        self.assertEqual(result, "USER: ping")


# ──────────────────────────────────────────────────────────────────────
#  Test: cmd_discard
# ──────────────────────────────────────────────────────────────────────

class TestCmdDiscard(unittest.TestCase):

    def _db(self) -> Database:
        return Database.open(":memory:")

    def test_discard_no_worktree_just_deletes_metadata(self):
        db = self._db()
        create_actor(db, "test", config=[])
        pm = FakeProcessManager()
        cmd_discard(db, pm, name="test")
        with self.assertRaises(NotFound):
            db.get_actor("test")

    def test_discard_worktree_just_deletes_metadata(self):
        db = self._db()
        git = FakeGit()
        create_actor_with_worktree(db, git, "feature")
        git.calls.clear()
        pm = FakeProcessManager()
        cmd_discard(db, pm, name="feature")
        # No git operations — worktree stays on disk
        self.assertEqual(len(git.calls), 0)
        with self.assertRaises(NotFound):
            db.get_actor("feature")

    def test_discard_force_stops_running_actor(self):
        db = self._db()
        create_actor(db, "test", config=[])
        pm = FakeProcessManager()
        run = Run(
            id=0, actor_name="test", prompt="go", status=Status.RUNNING,
            exit_code=None, pid=999, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        db.insert_run(run)
        pm.mark_alive(999)
        cmd_discard(db, pm, name="test")
        self.assertFalse(pm.is_alive(999))
        with self.assertRaises(NotFound):
            db.get_actor("test")

    def test_discard_detects_stale_pid_and_proceeds(self):
        db = self._db()
        create_actor(db, "test", config=[])
        pm = FakeProcessManager()
        run = Run(
            id=0, actor_name="test", prompt="go", status=Status.RUNNING,
            exit_code=None, pid=888, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        db.insert_run(run)
        # PID 888 is NOT alive
        cmd_discard(db, pm, name="test")
        with self.assertRaises(NotFound):
            db.get_actor("test")

    def test_discard_not_found(self):
        db = self._db()
        pm = FakeProcessManager()
        with self.assertRaises(NotFound):
            cmd_discard(db, pm, name="nope")

    def test_discard_deletes_runs_too(self):
        db = self._db()
        create_actor(db, "test", config=[])
        run = Run(
            id=0, actor_name="test", prompt="go", status=Status.DONE,
            exit_code=0, pid=1, config={},
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )
        db.insert_run(run)
        pm = FakeProcessManager()
        cmd_discard(db, pm, name="test")
        self.assertIsNone(db.latest_run("test"))

    def test_discard_cascades_to_children(self):
        db = self._db()
        create_actor(db, "parent", config=[])
        # Manually insert children with parent set
        child1 = make_actor("child1", parent="parent", dir="/tmp")
        child2 = make_actor("child2", parent="parent", dir="/tmp")
        db.insert_actor(child1)
        db.insert_actor(child2)
        pm = FakeProcessManager()
        result = cmd_discard(db, pm, name="parent")
        self.assertIn("child1 discarded", result)
        self.assertIn("child2 discarded", result)
        self.assertIn("parent discarded", result)
        with self.assertRaises(NotFound):
            db.get_actor("child1")
        with self.assertRaises(NotFound):
            db.get_actor("child2")
        with self.assertRaises(NotFound):
            db.get_actor("parent")

    def test_discard_cascades_recursively(self):
        db = self._db()
        create_actor(db, "root", config=[])
        child = make_actor("child", parent="root", dir="/tmp")
        grandchild = make_actor("grandchild", parent="child", dir="/tmp")
        db.insert_actor(child)
        db.insert_actor(grandchild)
        pm = FakeProcessManager()
        result = cmd_discard(db, pm, name="root")
        self.assertIn("grandchild discarded", result)
        self.assertIn("child discarded", result)
        self.assertIn("root discarded", result)
        for name in ["root", "child", "grandchild"]:
            with self.assertRaises(NotFound):
                db.get_actor(name)

    def test_discard_stops_running_children(self):
        db = self._db()
        create_actor(db, "parent", config=[])
        child = make_actor("child", parent="parent", dir="/tmp")
        db.insert_actor(child)
        run = Run(
            id=0, actor_name="child", prompt="go", status=Status.RUNNING,
            exit_code=None, pid=777, config={},
            started_at="2026-01-01T00:00:00Z", finished_at=None,
        )
        db.insert_run(run)
        pm = FakeProcessManager()
        pm.mark_alive(777)
        cmd_discard(db, pm, name="parent")
        self.assertFalse(pm.is_alive(777))
        with self.assertRaises(NotFound):
            db.get_actor("child")
        with self.assertRaises(NotFound):
            db.get_actor("parent")


# ──────────────────────────────────────────────────────────────────────
#  Test: ClaudeAgent  (16 tests from agents/claude.rs)
# ──────────────────────────────────────────────────────────────────────

class TestClaudeAgent(unittest.TestCase):
    """Tests for Claude-specific logic: encode_dir, session_file_path,
    config_args, and JSONL read_logs parsing."""

    # -- encode_dir tests (2) --

    def test_encode_dir_replaces_non_alnum(self):
        # The Python equivalent of ClaudeAgent::encode_dir
        from actor import encode_dir
        encoded = encode_dir("/Users/mme/Projects/actor.sh/main")
        self.assertEqual(encoded, "-Users-mme-Projects-actor-sh-main")

    def test_encode_dir_preserves_alnum(self):
        from actor import encode_dir
        encoded = encode_dir("/abc123")
        self.assertEqual(encoded, "-abc123")

    # -- session_file_path test (1) --

    def test_session_file_path_correct(self):
        from actor import claude_session_file_path
        path = claude_session_file_path("/Users/mme/Projects/mysite", "abc-123")
        home = os.environ["HOME"]
        expected = os.path.join(home, ".claude", "projects",
                                "-Users-mme-Projects-mysite", "abc-123.jsonl")
        self.assertEqual(path, expected)

    # -- config_args test (1) --

    def test_config_args_builds_flags(self):
        from actor import claude_config_args
        config = {"model": "sonnet", "max-budget-usd": "5"}
        args = claude_config_args(config)
        # Sorted keys: max-budget-usd before model
        self.assertEqual(args, ["--max-budget-usd", "5", "--model", "sonnet"])

    # -- read_logs tests (12) --

    def _write_jsonl(self, content: str) -> str:
        """Write JSONL content to a temp file and return the path."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_read_logs_missing_file_returns_empty(self):
        from actor import claude_read_logs
        entries = claude_read_logs("/nonexistent/path/that/does/not/exist.jsonl")
        self.assertEqual(entries, [])

    def test_read_logs_user_message_string_content(self):
        from actor import claude_read_logs
        jsonl = '{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello world"}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].kind, LogEntryKind.USER)
            self.assertEqual(entries[0].text, "hello world")
            self.assertEqual(entries[0].timestamp, "2026-01-01T00:00:00Z")
        finally:
            os.unlink(path)

    def test_read_logs_user_message_array_content_extracts_tool_results(self):
        from actor import claude_read_logs
        jsonl = '{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":[{"type":"tool_result","tool_use_id":"abc","content":"ok"}]}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].kind, LogEntryKind.TOOL_RESULT)
            self.assertEqual(entries[0].content, "ok")
        finally:
            os.unlink(path)

    def test_read_logs_assistant_single_text_block(self):
        from actor import claude_read_logs
        jsonl = '{"type":"assistant","timestamp":"2026-01-01T00:01:00Z","message":{"content":[{"type":"text","text":"I can help with that."}]}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[0].text, "I can help with that.")
        finally:
            os.unlink(path)

    def test_read_logs_assistant_multiple_text_blocks_separate_entries(self):
        from actor import claude_read_logs
        jsonl = '{"type":"assistant","timestamp":"2026-01-01T00:01:00Z","message":{"content":[{"type":"text","text":"First paragraph."},{"type":"text","text":"Second paragraph."}]}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[0].text, "First paragraph.")
            self.assertEqual(entries[1].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[1].text, "Second paragraph.")
        finally:
            os.unlink(path)

    def test_read_logs_assistant_mixed_text_and_tool_use(self):
        from actor import claude_read_logs
        jsonl = '{"type":"assistant","timestamp":"2026-01-01T00:01:00Z","message":{"content":[{"type":"text","text":"Let me check."},{"type":"tool_use","id":"tu1","name":"Bash","input":{"command":"ls"}},{"type":"text","text":"Here are the results."}]}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[0].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[0].text, "Let me check.")
            self.assertEqual(entries[1].kind, LogEntryKind.TOOL_USE)
            self.assertEqual(entries[1].name, "Bash")
            self.assertEqual(entries[2].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[2].text, "Here are the results.")
        finally:
            os.unlink(path)

    def test_read_logs_thinking_block(self):
        from actor import claude_read_logs
        jsonl = '{"type":"assistant","timestamp":"2026-01-01T00:01:00Z","message":{"content":[{"type":"thinking","thinking":"Let me reason about this..."},{"type":"text","text":"Here\'s my answer."}]}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].kind, LogEntryKind.THINKING)
            self.assertEqual(entries[0].text, "Let me reason about this...")
            self.assertEqual(entries[1].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[1].text, "Here's my answer.")
        finally:
            os.unlink(path)

    def test_read_logs_malformed_json_skipped(self):
        from actor import claude_read_logs
        jsonl = 'this is not valid json\n{"type":"user","message":{"content":"valid"}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].kind, LogEntryKind.USER)
            self.assertEqual(entries[0].text, "valid")
        finally:
            os.unlink(path)

    def test_read_logs_missing_type_field_skipped(self):
        from actor import claude_read_logs
        jsonl = '{"message":{"content":"no type field"}}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 0)
        finally:
            os.unlink(path)

    def test_read_logs_missing_message_field_skipped(self):
        from actor import claude_read_logs
        jsonl = '{"type":"user","timestamp":"2026-01-01T00:00:00Z"}'
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 0)
        finally:
            os.unlink(path)

    def test_read_logs_empty_file(self):
        from actor import claude_read_logs
        path = self._write_jsonl("")
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 0)
        finally:
            os.unlink(path)

    def test_read_logs_multiple_messages_order_preserved(self):
        from actor import claude_read_logs
        jsonl = (
            '{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"first question"}}\n'
            '{"type":"assistant","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"first answer"}]}}\n'
            '{"type":"user","timestamp":"2026-01-01T00:00:02Z","message":{"content":"second question"}}\n'
            '{"type":"assistant","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"text","text":"second answer"}]}}'
        )
        path = self._write_jsonl(jsonl)
        try:
            entries = claude_read_logs(path)
            self.assertEqual(len(entries), 4)
            self.assertEqual(entries[0].kind, LogEntryKind.USER)
            self.assertEqual(entries[0].text, "first question")
            self.assertEqual(entries[1].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[1].text, "first answer")
            self.assertEqual(entries[2].kind, LogEntryKind.USER)
            self.assertEqual(entries[2].text, "second question")
            self.assertEqual(entries[3].kind, LogEntryKind.ASSISTANT)
            self.assertEqual(entries[3].text, "second answer")
        finally:
            os.unlink(path)


# ──────────────────────────────────────────────────────────────────────
#  Test: CodexAgent  (4 tests from agents/codex.rs)
# ──────────────────────────────────────────────────────────────────────

class TestCodexAgent(unittest.TestCase):

    def test_config_args_model_becomes_m_flag(self):
        from actor import codex_config_args
        config = {"model": "o3"}
        args = codex_config_args(config)
        self.assertEqual(args, ["-m", "o3"])

    def test_config_args_other_keys_become_c_flags(self):
        from actor import codex_config_args
        config = {"instructions": "be helpful"}
        args = codex_config_args(config)
        self.assertEqual(args, ["-c", "instructions=be helpful"])

    def test_config_args_mixed(self):
        from actor import codex_config_args
        config = {"model": "o3", "sandbox_mode": "workspace-write"}
        args = codex_config_args(config)
        # Sorted keys: model < sandbox_mode
        self.assertEqual(args, ["-m", "o3", "-c", "sandbox_mode=workspace-write"])

    def test_parse_thread_started(self):
        line = '{"type":"thread.started","thread_id":"019d685b-6ed6-7f72-bdaa-19533aad1f43"}'
        v = json.loads(line)
        thread_id = v.get("thread_id")
        self.assertEqual(thread_id, "019d685b-6ed6-7f72-bdaa-19533aad1f43")


# ──────────────────────────────────────────────────────────────────────
#  Test: hooks.py (runner + env builder) — issue #30
# ──────────────────────────────────────────────────────────────────────

class TestHookEnv(unittest.TestCase):

    def test_hook_env_sets_required_vars(self):
        from pathlib import Path
        from actor.hooks import hook_env
        env = hook_env(
            {"PATH": "/bin"},
            actor_name="foo",
            actor_dir=Path("/tmp/foo"),
            actor_agent="claude",
            actor_session_id=None,
        )
        self.assertEqual(env["ACTOR_NAME"], "foo")
        self.assertEqual(env["ACTOR_DIR"], "/tmp/foo")
        self.assertEqual(env["ACTOR_AGENT"], "claude")
        self.assertNotIn("ACTOR_SESSION_ID", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_hook_env_includes_session_id_when_set(self):
        from pathlib import Path
        from actor.hooks import hook_env
        env = hook_env(
            {},
            actor_name="foo",
            actor_dir=Path("/tmp/foo"),
            actor_agent="codex",
            actor_session_id="sess-123",
        )
        self.assertEqual(env["ACTOR_SESSION_ID"], "sess-123")

    def test_hook_env_does_not_mutate_base_env(self):
        from pathlib import Path
        from actor.hooks import hook_env
        base = {"PATH": "/bin"}
        hook_env(
            base,
            actor_name="foo",
            actor_dir=Path("/tmp"),
            actor_agent="claude",
            actor_session_id=None,
        )
        self.assertNotIn("ACTOR_NAME", base)


class TestRunHook(unittest.TestCase):

    def test_run_hook_no_command_is_noop(self):
        from pathlib import Path
        from actor.hooks import run_hook
        calls = []

        def runner(cmd, env, cwd):
            calls.append((cmd, env, cwd))
            return 0

        run_hook("on-start", None, {}, Path("/tmp"), runner=runner)
        self.assertEqual(calls, [])

    def test_run_hook_success_returns_none(self):
        from pathlib import Path
        from actor.hooks import run_hook

        def runner(cmd, env, cwd):
            return 0

        # Should not raise.
        run_hook("on-start", "echo hi", {}, Path("/tmp"), runner=runner)

    def test_run_hook_nonzero_raises_hook_failed(self):
        from pathlib import Path
        from actor.hooks import run_hook
        from actor.errors import HookFailedError

        def runner(cmd, env, cwd):
            return 3

        with self.assertRaises(HookFailedError) as ctx:
            run_hook("on-run", "false", {}, Path("/tmp"), runner=runner)
        self.assertEqual(ctx.exception.event, "on-run")
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertEqual(ctx.exception.command, "false")

    def test_run_hook_passes_env_and_cwd_to_runner(self):
        from pathlib import Path
        from actor.hooks import run_hook
        seen = {}

        def runner(cmd, env, cwd):
            seen["cmd"] = cmd
            seen["env"] = dict(env)
            seen["cwd"] = cwd
            return 0

        run_hook(
            "on-start", "echo",
            env={"FOO": "bar"}, cwd=Path("/tmp/x"), runner=runner,
        )
        self.assertEqual(seen["cmd"], "echo")
        self.assertEqual(seen["env"], {"FOO": "bar"})
        self.assertEqual(seen["cwd"], Path("/tmp/x"))


class TestDefaultHookRunner(unittest.TestCase):
    """Exercises the real subprocess path with trivial shell commands."""

    def test_default_runner_zero_exit(self):
        from pathlib import Path
        from actor.hooks import _default_hook_runner
        with tempfile.TemporaryDirectory() as d:
            rc = _default_hook_runner("true", dict(os.environ), Path(d))
            self.assertEqual(rc, 0)

    def test_default_runner_nonzero_exit(self):
        from pathlib import Path
        from actor.hooks import _default_hook_runner
        with tempfile.TemporaryDirectory() as d:
            rc = _default_hook_runner("false", dict(os.environ), Path(d))
            self.assertNotEqual(rc, 0)

    def test_default_runner_sees_env(self):
        from pathlib import Path
        from actor.hooks import _default_hook_runner
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ)
            env["HOOK_TEST_VAR"] = "xyz"
            rc = _default_hook_runner(
                '[ "$HOOK_TEST_VAR" = "xyz" ]', env, Path(d),
            )
            self.assertEqual(rc, 0)

    def test_default_runner_cwd_is_honored(self):
        from pathlib import Path
        from actor.hooks import _default_hook_runner
        with tempfile.TemporaryDirectory() as d:
            real = str(Path(d).resolve())
            rc = _default_hook_runner(
                f'[ "$(pwd -P)" = "{real}" ]', dict(os.environ), Path(d),
            )
            self.assertEqual(rc, 0)


# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main()
