"""Lifecycle hook tests.

Covers: hooks.py runtime (run_hook, hook_env, merge_env_extra), KDL
parsing of the `hooks { }` block, and the wiring in cmd_new / cmd_run /
cmd_interactive / cmd_discard."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple

from actor import (
    Database,
    Status,
    cmd_discard,
    cmd_new,
    cmd_run,
    cmd_interactive,
)
from actor.config import AppConfig, Hooks, load_config
from actor.errors import ConfigError, HookFailedError
from actor.hooks import HookResult, hook_env, merge_env_extra, run_hook

# Reuse the fakes from test_actor.py so we don't duplicate a test harness.
from tests.test_actor import (
    FakeAgent,
    FakeGit,
    FakeProcessManager,
    _cli,
    create_actor,
)


def _in_memory_db() -> Database:
    return Database.open(":memory:")


class _RecordingRunner:
    """Test double for HookRunner. Records calls; returns a scripted outcome."""

    def __init__(self, outcomes: List[int] | List[HookResult] | int = 0) -> None:
        if isinstance(outcomes, list):
            self._outcomes: list = list(outcomes)
        else:
            self._outcomes = [outcomes]
        self.calls: List[Tuple[str, dict, Path]] = []

    def __call__(self, command: str, env, cwd: Path):
        self.calls.append((command, dict(env), cwd))
        if not self._outcomes:
            return 0
        return self._outcomes.pop(0)


# ──────────────────────────────────────────────────────────────────────
#  hooks.py — run_hook
# ──────────────────────────────────────────────────────────────────────

class TestRunHook(unittest.TestCase):

    def test_none_command_is_noop(self):
        # No runner invoked; no error.
        run_hook("on-start", None, {}, Path("/tmp"))

    def test_zero_exit_succeeds(self):
        runner = _RecordingRunner(0)
        run_hook("on-start", "echo hi", {"K": "V"}, Path("/tmp"), runner=runner)
        self.assertEqual(len(runner.calls), 1)
        cmd, env, cwd = runner.calls[0]
        self.assertEqual(cmd, "echo hi")
        self.assertEqual(env["K"], "V")
        self.assertEqual(cwd, Path("/tmp"))

    def test_nonzero_exit_raises_hook_failed(self):
        runner = _RecordingRunner(2)
        with self.assertRaises(HookFailedError) as ctx:
            run_hook("on-start", "false", {}, Path("/tmp"), runner=runner)
        self.assertEqual(ctx.exception.event, "on-start")
        self.assertEqual(ctx.exception.exit_code, 2)
        self.assertEqual(ctx.exception.command, "false")

    def test_hook_result_stdout_stderr_carried_to_error(self):
        runner = _RecordingRunner([HookResult(exit_code=7, stdout="out", stderr="boom")])
        with self.assertRaises(HookFailedError) as ctx:
            run_hook("on-discard", "x", {}, Path("/tmp"), runner=runner)
        self.assertEqual(ctx.exception.stdout, "out")
        self.assertEqual(ctx.exception.stderr, "boom")
        self.assertIn("boom", str(ctx.exception))

    def test_bool_return_rejected(self):
        def bool_runner(cmd, env, cwd):
            return True  # type: ignore[return-value]
        with self.assertRaises(TypeError):
            run_hook("on-start", "x", {}, Path("/tmp"), runner=bool_runner)


class TestHookEnv(unittest.TestCase):

    def test_sets_actor_vars(self):
        out = hook_env(
            {"PATH": "/bin"},
            actor_name="foo",
            actor_dir=Path("/tmp/x"),
            actor_agent="claude",
            actor_session_id="sess-123",
        )
        self.assertEqual(out["ACTOR_NAME"], "foo")
        self.assertEqual(out["ACTOR_DIR"], "/tmp/x")
        self.assertEqual(out["ACTOR_AGENT"], "claude")
        self.assertEqual(out["ACTOR_SESSION_ID"], "sess-123")
        self.assertEqual(out["PATH"], "/bin")
        self.assertNotIn("ACTOR_RUN_ID", out)

    def test_session_id_none_pops_existing(self):
        out = hook_env(
            {"ACTOR_SESSION_ID": "stale"},
            actor_name="foo",
            actor_dir=Path("/tmp"),
            actor_agent="claude",
            actor_session_id=None,
        )
        self.assertNotIn("ACTOR_SESSION_ID", out)

    def test_run_vars_emitted_for_after_run(self):
        out = hook_env(
            {},
            actor_name="foo",
            actor_dir=Path("/tmp"),
            actor_agent="codex",
            actor_session_id=None,
            actor_run_id=42,
            actor_exit_code=0,
            actor_duration_ms=1234,
        )
        self.assertEqual(out["ACTOR_RUN_ID"], "42")
        self.assertEqual(out["ACTOR_EXIT_CODE"], "0")
        self.assertEqual(out["ACTOR_DURATION_MS"], "1234")

    def test_bool_for_int_vars_rejected(self):
        with self.assertRaises(TypeError):
            hook_env(
                {},
                actor_name="foo",
                actor_dir=Path("/tmp"),
                actor_agent="claude",
                actor_session_id=None,
                actor_exit_code=True,  # type: ignore[arg-type]
            )


class TestMergeEnvExtra(unittest.TestCase):

    def test_string_value_sets(self):
        env = {"A": "1"}
        merge_env_extra(env, {"B": "2"})
        self.assertEqual(env, {"A": "1", "B": "2"})

    def test_none_value_unsets(self):
        env = {"A": "1", "B": "2"}
        merge_env_extra(env, {"B": None})
        self.assertEqual(env, {"A": "1"})

    def test_non_string_rejected(self):
        env = {}
        with self.assertRaises(TypeError):
            merge_env_extra(env, {"K": 42})  # type: ignore[dict-item]


# ──────────────────────────────────────────────────────────────────────
#  KDL parsing for the hooks block
# ──────────────────────────────────────────────────────────────────────

class TestHooksConfigParse(unittest.TestCase):

    def _load(self, body: str) -> AppConfig:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            p = Path(cwd) / ".actor" / "settings.kdl"
            p.parent.mkdir()
            p.write_text(body)
            return load_config(cwd=Path(cwd), home=Path(home))

    def test_all_four_events(self):
        cfg = self._load(
            'hooks {\n'
            '    on-start "./setup.sh"\n'
            '    before-run "git fetch"\n'
            '    after-run "./report.sh"\n'
            '    on-discard "git diff --quiet"\n'
            '}\n'
        )
        self.assertEqual(cfg.hooks.on_start, "./setup.sh")
        self.assertEqual(cfg.hooks.before_run, "git fetch")
        self.assertEqual(cfg.hooks.after_run, "./report.sh")
        self.assertEqual(cfg.hooks.on_discard, "git diff --quiet")

    def test_empty_block_leaves_defaults(self):
        cfg = self._load("hooks { }\n")
        self.assertIsNone(cfg.hooks.on_start)

    def test_unknown_event_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('hooks { on-wat "x" }\n')

    def test_duplicate_event_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('hooks {\n    on-start "a"\n    on-start "b"\n}\n')

    def test_non_string_value_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('hooks { on-start 42 }\n')

    def test_project_overrides_user_per_event(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            (Path(home) / ".actor").mkdir()
            (Path(home) / ".actor" / "settings.kdl").write_text(
                'hooks {\n    on-start "user-start"\n    before-run "user-run"\n}\n'
            )
            (Path(cwd) / ".actor").mkdir()
            (Path(cwd) / ".actor" / "settings.kdl").write_text(
                'hooks {\n    before-run "proj-run"\n}\n'
            )
            cfg = load_config(cwd=Path(cwd), home=Path(home))
        self.assertEqual(cfg.hooks.on_start, "user-start")  # from user
        self.assertEqual(cfg.hooks.before_run, "proj-run")  # project wins


# ──────────────────────────────────────────────────────────────────────
#  Wiring: cmd_new on-start
# ──────────────────────────────────────────────────────────────────────

class TestCmdNewOnStart(unittest.TestCase):

    def test_on_start_fires_with_actor_env(self):
        db = _in_memory_db()
        runner = _RecordingRunner(0)
        cfg = AppConfig(hooks=Hooks(on_start='echo "going"'))
        cmd_new(
            db, FakeGit(),
            name="foo", dir="/tmp", no_worktree=True, base=None,
            agent_name="claude", cli_overrides=_cli(),
            app_config=cfg, hook_runner=runner,
        )
        self.assertEqual(len(runner.calls), 1)
        _, env, _ = runner.calls[0]
        self.assertEqual(env["ACTOR_NAME"], "foo")
        self.assertEqual(env["ACTOR_AGENT"], "claude")

    def test_on_start_failure_rolls_back_actor(self):
        db = _in_memory_db()
        runner = _RecordingRunner(2)
        cfg = AppConfig(hooks=Hooks(on_start='false'))
        with self.assertRaises(HookFailedError):
            cmd_new(
                db, FakeGit(),
                name="foo", dir="/tmp", no_worktree=True, base=None,
                agent_name="claude", cli_overrides=_cli(),
                app_config=cfg, hook_runner=runner,
            )
        # Actor row gone after rollback.
        self.assertEqual(db.list_actors(), [])


# ──────────────────────────────────────────────────────────────────────
#  Wiring: cmd_run before-run + after-run
# ──────────────────────────────────────────────────────────────────────

class TestCmdRunBeforeRun(unittest.TestCase):

    def test_before_run_veto_aborts_no_run_row(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        agent = FakeAgent()
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(before_run='false'))
        runner = _RecordingRunner(1)
        with self.assertRaises(HookFailedError):
            cmd_run(
                db, agent, pm,
                name="foo", prompt="do x", cli_overrides=_cli(),
                app_config=cfg, hook_runner=runner,
            )
        # No run inserted, no agent started.
        self.assertEqual(len(agent.calls), 0)
        self.assertIsNone(db.latest_run("foo"))

    def test_before_run_success_proceeds(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        agent = FakeAgent()
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(before_run='echo ok'))
        runner = _RecordingRunner(0)
        cmd_run(
            db, agent, pm,
            name="foo", prompt="do x", cli_overrides=_cli(),
            app_config=cfg, hook_runner=runner,
        )
        self.assertEqual(len(agent.calls), 1)


class TestCmdRunAfterRun(unittest.TestCase):

    def test_after_run_fires_after_db_update(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        agent = FakeAgent()
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(after_run='./report.sh'))
        runner = _RecordingRunner(0)
        cmd_run(
            db, agent, pm,
            name="foo", prompt="do x", cli_overrides=_cli(),
            app_config=cfg, hook_runner=runner,
        )
        self.assertEqual(len(runner.calls), 1)
        _, env, _ = runner.calls[0]
        # DB was updated first — run row should be DONE by now
        run = db.latest_run("foo")
        self.assertIsNotNone(run)
        self.assertEqual(run.status, Status.DONE)
        # after-run-specific env vars present
        self.assertIn("ACTOR_RUN_ID", env)
        self.assertEqual(env["ACTOR_EXIT_CODE"], "0")
        self.assertIn("ACTOR_DURATION_MS", env)

    def test_after_run_failure_does_not_abort(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        agent = FakeAgent()
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(after_run='false'))
        runner = _RecordingRunner(1)
        # No exception propagated; run still returns.
        output = cmd_run(
            db, agent, pm,
            name="foo", prompt="do x", cli_overrides=_cli(),
            app_config=cfg, hook_runner=runner,
        )
        self.assertEqual(output, "fake output")
        run = db.latest_run("foo")
        self.assertEqual(run.status, Status.DONE)


class TestCmdInteractiveHooks(unittest.TestCase):

    def _setup(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        # cmd_interactive requires a session, so run once to set it.
        agent = FakeAgent()
        pm = FakeProcessManager()
        cmd_run(
            db, agent, pm, name="foo", prompt="first", cli_overrides=_cli(),
        )
        return db, agent, pm

    def test_interactive_fires_before_and_after(self):
        db, agent, pm = self._setup()
        cfg = AppConfig(hooks=Hooks(before_run='a', after_run='b'))
        runner = _RecordingRunner([0, 0])
        def fake_runner(argv, cwd, env):
            return 0
        cmd_interactive(
            db, agent, pm, name="foo", runner=fake_runner,
            app_config=cfg, hook_runner=runner,
        )
        events = [c[0] for c in runner.calls]
        self.assertEqual(events, ["a", "b"])


# ──────────────────────────────────────────────────────────────────────
#  Wiring: cmd_discard on-discard
# ──────────────────────────────────────────────────────────────────────

class TestAfterRunInvariants(unittest.TestCase):
    """Cases that are easy to break when refactoring after-run wiring."""

    def test_after_run_skipped_when_before_run_vetoes(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        agent = FakeAgent()
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(before_run='bad', after_run='never'))
        events: list[str] = []
        def runner(cmd, env, cwd):
            events.append(cmd)
            return 1 if cmd == 'bad' else 0
        with self.assertRaises(HookFailedError):
            cmd_run(db, agent, pm, name="foo", prompt="x",
                    cli_overrides=_cli(), app_config=cfg, hook_runner=runner)
        # after-run never ran because before-run vetoed.
        self.assertEqual(events, ['bad'])

    def test_after_run_skipped_when_agent_start_raises(self):
        from actor.errors import ActorError as AE
        db = _in_memory_db()
        create_actor(db, "foo")
        agent = FakeAgent()
        agent.set_start_error(AE("boom"))
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(after_run='never'))
        events: list[str] = []
        def runner(cmd, env, cwd):
            events.append(cmd)
            return 0
        with self.assertRaises(AE):
            cmd_run(db, agent, pm, name="foo", prompt="x",
                    cli_overrides=_cli(), app_config=cfg, hook_runner=runner)
        self.assertEqual(events, [])

    def test_after_run_receives_nonzero_exit_code_env_var(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        agent = FakeAgent()
        agent.set_exit_code(2)
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(after_run='report'))
        runner = _RecordingRunner(0)
        cmd_run(db, agent, pm, name="foo", prompt="x",
                cli_overrides=_cli(), app_config=cfg, hook_runner=runner)
        _, env, _ = runner.calls[0]
        self.assertEqual(env["ACTOR_EXIT_CODE"], "2")


class TestCmdDiscardOnDiscard(unittest.TestCase):

    def test_on_discard_fires_before_delete(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(on_discard='git diff --quiet'))
        runner = _RecordingRunner(0)
        cmd_discard(
            db, pm, name="foo",
            app_config=cfg, hook_runner=runner,
        )
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(db.list_actors(), [])

    def test_on_discard_failure_aborts_without_force(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(on_discard='false'))
        runner = _RecordingRunner(1)
        with self.assertRaises(HookFailedError):
            cmd_discard(
                db, pm, name="foo",
                app_config=cfg, hook_runner=runner, force=False,
            )
        # Actor still in DB — not discarded.
        self.assertEqual(len(db.list_actors()), 1)

    def test_missing_worktree_falls_back_to_home_cwd(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        # Actor's dir is /tmp/foo (from create_actor no_worktree=True);
        # to hit the missing-dir branch we override the actor row to
        # point at a path that doesn't exist.
        actor = db.get_actor("foo")
        from actor import Actor
        db.update_actor_config(
            "foo", actor.config,
        )
        # Monkey-update the actor's dir via a targeted update (no helper
        # exists for this; use a direct connection). Acceptable because
        # the test just probes the on-discard branch behavior.
        import sqlite3
        # Database.open(":memory:") returns a wrapper; reach under the hood.
        db._conn.execute(  # type: ignore[attr-defined]
            "UPDATE actors SET dir=? WHERE name=?",
            ("/tmp/actor-does-not-exist-xyz", "foo"),
        )
        db._conn.commit()  # type: ignore[attr-defined]

        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(on_discard='pwd'))
        runner = _RecordingRunner(0)
        cmd_discard(db, pm, name="foo",
                    app_config=cfg, hook_runner=runner, force=False)
        _, _, cwd = runner.calls[0]
        self.assertEqual(cwd, Path.home())

    def test_force_bypasses_hook_failure(self):
        db = _in_memory_db()
        create_actor(db, "foo")
        pm = FakeProcessManager()
        cfg = AppConfig(hooks=Hooks(on_discard='false'))
        runner = _RecordingRunner(1)
        cmd_discard(
            db, pm, name="foo",
            app_config=cfg, hook_runner=runner, force=True,
        )
        # Discarded despite the failing hook.
        self.assertEqual(db.list_actors(), [])


if __name__ == "__main__":
    unittest.main()
