"""Tests for the CLI dispatch and MCP tool wrappers.

These test the thin glue layer on top of `LocalActorService` —
argument translation, prompt resolution (arg vs. stdin), error
handling, and the MCP tool signatures. The service layer itself is
covered by test_actor.py.
"""
from __future__ import annotations

import asyncio
import io
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from actor import ActorConfig, RunResult, Status, __version__
from actor.cli import main
from actor.errors import ActorError, ConfigError


def _run_async(coro):
    """Drive an async test body from a sync test method."""
    return asyncio.run(coro)


class VersionFlagTests(unittest.TestCase):
    """`actor --version` / `-V` prints the installed version and exits 0."""

    def _run(self, argv):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            try:
                main(argv)
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return buf.getvalue(), code

    def test_long_flag_prints_version(self):
        out, code = self._run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(f"actor-sh {__version__}", out)

    def test_short_flag_prints_version(self):
        out, code = self._run(["-V"])
        self.assertEqual(code, 0)
        self.assertIn(f"actor-sh {__version__}", out)


def _fake_run_result(actor_name="foo", output="done", exit_code=0):
    return RunResult(
        run_id=1, actor=actor_name,
        status=Status.DONE if exit_code == 0 else Status.ERROR,
        exit_code=exit_code, output=output,
    )


class NewCommandTests(unittest.TestCase):
    """`actor new` CLI dispatch."""

    def _run(self, argv, stdin_text=None, stdin_is_tty=True, app_config=None):
        """Invoke cli.main with argv, patching service methods. Returns
        (new_actor_mock, run_actor_mock, exit_code) so tests can probe
        the args the CLI translated argv into."""
        from actor import AppConfig
        cfg = app_config if app_config is not None else AppConfig()

        fake_actor = MagicMock()
        fake_actor.name = argv[1] if len(argv) > 1 else "a"
        fake_actor.dir = "/tmp/actor"

        # AsyncMock so the CLI's `await service.new_actor(...)` /
        # `await service.run_actor(...)` resolve to the fake values
        # rather than coroutines that never finish.
        new_actor = AsyncMock(return_value=fake_actor)
        run_actor = AsyncMock(return_value=_fake_run_result())

        stdin = io.StringIO(stdin_text or "")
        stdin.isatty = lambda: stdin_is_tty  # type: ignore[assignment]

        # `_propagate_run_exit` looks up the latest run from the
        # service after run_actor returns; mock that to a benign run.
        latest_run = MagicMock(exit_code=0)

        with patch("actor.service.RemoteActorService.new_actor", new_actor), \
             patch("actor.service.RemoteActorService.run_actor", run_actor), \
             patch("actor.service.RemoteActorService.latest_run", new=AsyncMock(return_value=latest_run)), \
             patch("actor.config.load_config", return_value=cfg), \
             patch("actor.cli.Database") as db_cls, \
             patch("sys.stdin", stdin):
            db_cls.open.return_value = MagicMock()
            try:
                main(argv)
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code if isinstance(e.code, int) else 1
        return new_actor, run_actor, exit_code

    def test_new_without_prompt_creates_only(self):
        new_actor, run_actor, code = self._run(["new", "foo"])
        new_actor.assert_called_once()
        run_actor.assert_not_called()
        self.assertEqual(code, 0)

    def test_new_with_prompt_arg_creates_and_runs(self):
        new_actor, run_actor, code = self._run(["new", "foo", "do x"])
        new_actor.assert_called_once()
        run_actor.assert_called_once()
        kwargs = run_actor.call_args.kwargs
        self.assertEqual(kwargs["name"], "foo")
        self.assertEqual(kwargs["prompt"], "do x")
        self.assertEqual(kwargs["config"], ActorConfig())
        self.assertEqual(code, 0)

    def test_new_with_stdin_prompt_creates_and_runs(self):
        new_actor, run_actor, code = self._run(
            ["new", "foo"], stdin_text="fix it\n", stdin_is_tty=False,
        )
        new_actor.assert_called_once()
        run_actor.assert_called_once()
        self.assertEqual(run_actor.call_args.kwargs["prompt"], "fix it")
        self.assertEqual(code, 0)

    def test_new_with_empty_stdin_errors(self):
        new_actor, run_actor, code = self._run(
            ["new", "foo"], stdin_text="", stdin_is_tty=False,
        )
        new_actor.assert_called_once()
        run_actor.assert_not_called()
        self.assertEqual(code, 1)

    def test_new_translates_model_and_use_subscription_to_config(self):
        new_actor, _run, _code = self._run([
            "new", "foo", "--model", "sonnet", "--no-use-subscription",
        ])
        config = new_actor.call_args.kwargs["config"]
        self.assertEqual(config.agent_args.get("model"), "sonnet")
        self.assertEqual(config.actor_keys.get("use-subscription"), "false")

    def test_new_without_use_subscription_flag_does_not_emit_override(self):
        """Tri-state check: omitting both --use-subscription and
        --no-use-subscription must NOT push a use-subscription value,
        so a role's value wins."""
        new_actor, _run, _code = self._run(["new", "foo"])
        config = new_actor.call_args.kwargs["config"]
        self.assertNotIn(
            "use-subscription", config.actor_keys,
            f"expected no use-subscription override, got {config.actor_keys}",
        )

    def test_new_explicit_use_subscription_flag_emits_true_override(self):
        """Explicit --use-subscription must push use-subscription=true
        so it can override a role's use-subscription "false"."""
        new_actor, _run, _code = self._run(["new", "foo", "--use-subscription"])
        config = new_actor.call_args.kwargs["config"]
        self.assertEqual(config.actor_keys.get("use-subscription"), "true")

    def test_new_passes_role_arg_to_new_actor(self):
        # `actor new foo --role qa` (no prompt) creates the actor with
        # the role applied but does not auto-run — the role's `prompt`
        # is the actor's system prompt, not a fallback task.
        from actor import AppConfig, Role
        cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })
        new_actor, run_actor, code = self._run(
            ["new", "foo", "--role", "qa"], app_config=cfg,
        )
        new_actor.assert_called_once()
        kwargs = new_actor.call_args.kwargs
        self.assertEqual(kwargs["role_name"], "qa")
        self.assertEqual(code, 0)
        run_actor.assert_not_called()

    def test_new_with_role_and_explicit_prompt_runs_with_explicit_prompt(self):
        from actor import AppConfig, Role
        cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })
        _new, run_actor, _code = self._run(
            ["new", "foo", "review the auth module", "--role", "qa"],
            app_config=cfg,
        )
        self.assertEqual(
            run_actor.call_args.kwargs["prompt"], "review the auth module",
        )

    def test_new_without_role_does_not_pass_role_kwargs(self):
        """Regression check: normal `actor new foo` must still work end-to-end."""
        new_actor, _run, code = self._run(["new", "foo"])
        kwargs = new_actor.call_args.kwargs
        self.assertIsNone(kwargs["role_name"])
        self.assertEqual(code, 0)

    def test_new_empty_stdin_with_role_does_not_run(self):
        """`echo "" | actor new foo --role qa` creates the actor without
        auto-running — empty stdin no longer falls back to the role's
        prompt (which is now a system prompt, not a task)."""
        from actor import AppConfig, Role
        cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })
        with patch("sys.stderr", io.StringIO()):
            _new, run_actor, code = self._run(
                ["new", "foo", "--role", "qa"],
                stdin_text="", stdin_is_tty=False, app_config=cfg,
            )
        self.assertEqual(code, 1)
        run_actor.assert_not_called()

    def test_new_surfaces_config_error_from_load_config(self):
        """A malformed settings.kdl must exit non-zero with the error
        text on stderr, not crash with an uncaught ConfigError."""
        with patch(
            "actor.config.load_config",
            side_effect=ConfigError("parse error in /x/settings.kdl: boom"),
        ):
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                try:
                    main(["new", "foo", "do x"])
                    code = 0
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        self.assertEqual(code, 1)
        self.assertIn("parse error", stderr.getvalue())

    def test_new_with_prompt_run_failure_surfaces_partial_success(self):
        from actor import AppConfig
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/foo"

        new_actor = AsyncMock(return_value=fake_actor)
        run_actor = AsyncMock(side_effect=ActorError("agent binary missing"))

        with patch("actor.service.RemoteActorService.new_actor", new_actor), \
             patch("actor.service.RemoteActorService.run_actor", run_actor), \
             patch("actor.config.load_config", return_value=AppConfig()), \
             patch("actor.cli.Database") as db_cls:
            db_cls.open.return_value = MagicMock()
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                try:
                    main(["new", "foo", "do x"])
                    code = 0
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        self.assertEqual(code, 2)
        self.assertIn("actor created but run failed", stderr.getvalue())


class RunCommandTests(unittest.TestCase):
    def test_run_passes_config_overrides(self):
        from actor import AppConfig
        from actor.types import AgentKind
        run_actor = AsyncMock(return_value=_fake_run_result())
        actor_row = MagicMock()
        actor_row.agent = AgentKind.CLAUDE
        get_actor = AsyncMock(return_value=actor_row)
        latest_run = MagicMock(exit_code=0)
        with patch("actor.service.RemoteActorService.run_actor", run_actor), \
             patch("actor.service.RemoteActorService.get_actor", get_actor), \
             patch("actor.service.RemoteActorService.latest_run", new=AsyncMock(return_value=latest_run)), \
             patch("actor.config.load_config", return_value=AppConfig()), \
             patch("actor.cli.Database") as db_cls:
            db_cls.open.return_value = MagicMock()
            stdin = io.StringIO("")
            stdin.isatty = lambda: True  # type: ignore[assignment]
            with patch("sys.stdin", stdin):
                main(["run", "foo", "--config", "model=opus", "do x"])
        run_actor.assert_called_once()
        config = run_actor.call_args.kwargs["config"]
        self.assertEqual(config.agent_args.get("model"), "opus")
        self.assertEqual(config.actor_keys, {})

    def test_run_dash_i_dispatches_to_interactive_session(self):
        """`actor run foo -i` must route through the gRPC bidi
        InteractiveSession, not run_actor."""
        from actor import AppConfig
        interactive_cli = AsyncMock(return_value=(0, "ok"))
        run_actor = AsyncMock(return_value=_fake_run_result(output="should-not-run"))
        with patch("actor.interactive.run_interactive_cli", interactive_cli), \
             patch("actor.service.RemoteActorService.run_actor", run_actor), \
             patch("actor.config.load_config", return_value=AppConfig()):
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main(["run", "foo", "-i"])
        self.assertEqual(ctx.exception.code, 0)
        interactive_cli.assert_called_once()
        self.assertEqual(interactive_cli.call_args.kwargs.get("name"), "foo")
        run_actor.assert_not_called()

    def test_run_dash_i_maps_signal_to_posix_exit(self):
        """Negative exit code from the interactive driver (signal) → 128 + signum."""
        import signal as _sig
        from actor import AppConfig
        interactive_cli = AsyncMock(return_value=(-_sig.SIGTERM, "stopped"))
        with patch("actor.interactive.run_interactive_cli", interactive_cli), \
             patch("actor.config.load_config", return_value=AppConfig()):
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main(["run", "foo", "-i"])
        self.assertEqual(ctx.exception.code, 128 + _sig.SIGTERM)

    def test_run_without_prompt_and_tty_exits_nonzero(self):
        from actor import AppConfig
        with patch("actor.config.load_config", return_value=AppConfig()), \
             patch("actor.cli.Database") as db_cls:
            db_cls.open.return_value = MagicMock()
            stdin = io.StringIO("")
            stdin.isatty = lambda: True  # type: ignore[assignment]
            with patch("sys.stdin", stdin), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main(["run", "foo"])
                self.assertEqual(ctx.exception.code, 1)


class MainCommandTests(unittest.TestCase):
    """`actor main` execs claude with the main role's prompt + actor channel."""

    def _patch_main_role(self, agent="claude", prompt="ROLE PROMPT"):
        from actor import AppConfig, Role
        cfg = AppConfig(roles={"main": Role(name="main", agent=agent, prompt=prompt)})
        return patch("actor.config.load_config", return_value=cfg)

    def test_execs_claude_with_channel_and_main_prompt(self):
        with self._patch_main_role(), patch("os.execvp") as execvp:
            main(["main"])
        argv = execvp.call_args.args[1]
        self.assertEqual(argv[0], "claude")
        self.assertIn("--dangerously-load-development-channels", argv)
        self.assertIn("server:actor", argv)
        # System prompt is appended via the --append-system-prompt flag
        # so it layers on top of Claude Code's defaults instead of
        # replacing them.
        self.assertIn("--append-system-prompt", argv)
        idx = argv.index("--append-system-prompt")
        self.assertEqual(argv[idx + 1], "ROLE PROMPT")

    def test_forwards_trailing_args_verbatim(self):
        with self._patch_main_role(), patch("os.execvp") as execvp:
            main(["main", "--model", "opus", "kick off the refactor"])
        argv = execvp.call_args.args[1]
        # Trailing args land after the channel + system-prompt pair.
        self.assertEqual(argv[-3:], ["--model", "opus", "kick off the refactor"])

    def test_missing_claude_binary_exits_cleanly(self):
        with self._patch_main_role(), \
             patch("os.execvp", side_effect=FileNotFoundError), \
             patch("sys.stderr", io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                main(["main"])
            self.assertEqual(ctx.exception.code, 1)
        self.assertIn("claude", err.getvalue().lower())

    def test_role_with_non_claude_agent_rejected(self):
        # `actor main` is claude-only today; if the user overrode main
        # to use codex, fail loudly rather than silently doing the
        # wrong thing.
        with self._patch_main_role(agent="codex"), \
             patch("os.execvp") as execvp, \
             patch("sys.stderr", io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                main(["main"])
            self.assertEqual(ctx.exception.code, 1)
        execvp.assert_not_called()
        self.assertIn("codex", err.getvalue())

    def test_role_without_prompt_omits_append_flag(self):
        # A `main` role with no prompt set still launches the
        # orchestrator session — just without the system-prompt append.
        with self._patch_main_role(prompt=None), patch("os.execvp") as execvp:
            main(["main"])
        argv = execvp.call_args.args[1]
        self.assertNotIn("--append-system-prompt", argv)

    def test_prompt_passed_verbatim_no_shell_escaping(self):
        # execvp passes argv entries directly to the child process —
        # no shell, no quoting, no interpolation. Tricky characters
        # that would need escaping in a shell command must reach the
        # child verbatim as one argv entry.
        tricky = (
            "Line one\n"
            "Line two with \"double\" and 'single' quotes\n"
            "Shell metas: $HOME `whoami` \\$(echo nope) | & ; > <\n"
            "Backslash: \\n is literal here, not a newline"
        )
        with self._patch_main_role(prompt=tricky), patch("os.execvp") as execvp:
            main(["main"])
        argv = execvp.call_args.args[1]
        idx = argv.index("--append-system-prompt")
        # The whole prompt is one argv entry, byte-identical to what we set.
        self.assertEqual(argv[idx + 1], tricky)
        self.assertEqual(argv.count("--append-system-prompt"), 1)


class SubClaudeChannelTests(unittest.TestCase):
    """ClaudeAgent must launch sub-claudes with the channel flag so
    nested actors receive completion notifications identically to the
    top-level session."""

    def test_start_includes_channel_flag(self):
        from actor.agents.claude import ClaudeAgent
        from pathlib import Path
        agent = ClaudeAgent()

        captured = {}

        async def fake_spawn(self, args, cwd, config):
            captured["args"] = args
            return 12345

        with patch.object(ClaudeAgent, "_spawn_and_track", fake_spawn):
            _run_async(agent.start(Path("/tmp"), "hi", ActorConfig()))

        self.assertIn("--dangerously-load-development-channels", captured["args"])
        idx = captured["args"].index("--dangerously-load-development-channels")
        self.assertEqual(captured["args"][idx + 1], "server:actor")

    def test_resume_includes_channel_flag(self):
        from actor.agents.claude import ClaudeAgent
        from pathlib import Path
        agent = ClaudeAgent()

        captured = {}

        async def fake_spawn(self, args, cwd, config):
            captured["args"] = args
            return 12345

        with patch.object(ClaudeAgent, "_spawn_and_track", fake_spawn):
            _run_async(agent.resume(Path("/tmp"), "some-session", "continue", ActorConfig()))

        self.assertIn("--dangerously-load-development-channels", captured["args"])


class McpToolTests(unittest.TestCase):
    """Exercise new_actor / run_actor wrappers."""

    def test_run_actor_strips_and_rejects_whitespace_prompt(self):
        from actor.server import run_actor
        with self.assertRaises(ActorError):
            _run_async(run_actor(name="foo", prompt="   "))

    def test_new_actor_without_prompt_does_not_spawn(self):
        from actor import server
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        with patch("actor.service.RemoteActorService.new_actor", AsyncMock(return_value=fake_actor)), \
             patch("actor.server._spawn_background_run") as spawn:
            msg = _run_async(server.new_actor(name="foo"))
        spawn.assert_not_called()
        self.assertIn("created", msg)
        self.assertNotIn("running", msg)

    def test_new_actor_with_whitespace_prompt_does_not_spawn(self):
        from actor import server
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        with patch("actor.service.RemoteActorService.new_actor", AsyncMock(return_value=fake_actor)), \
             patch("actor.server._spawn_background_run") as spawn:
            msg = _run_async(server.new_actor(name="foo", prompt="   "))
        spawn.assert_not_called()
        self.assertIn("created", msg)
        self.assertNotIn("running", msg)

    def test_new_actor_with_prompt_spawns_and_reports(self):
        from actor import server
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        with patch("actor.service.RemoteActorService.new_actor", AsyncMock(return_value=fake_actor)), \
             patch("actor.server._spawn_background_run") as spawn:
            msg = _run_async(server.new_actor(name="foo", prompt="do x"))
        spawn.assert_called_once()
        self.assertIn("running", msg)

    def test_new_actor_spawn_failure_reports_partial_success(self):
        from actor import server
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        with patch("actor.service.RemoteActorService.new_actor", AsyncMock(return_value=fake_actor)), \
             patch("actor.server._spawn_background_run", side_effect=RuntimeError("boom")):
            with patch("sys.stderr", io.StringIO()):
                msg = _run_async(server.new_actor(name="foo", prompt="do x"))
        self.assertIn("created", msg)
        self.assertIn("run failed to start", msg)

    def test_new_actor_passes_role_to_new_actor_no_auto_run(self):
        # `new_actor(role="qa")` with no prompt creates an idle actor
        # — the role's `prompt` is its system prompt, not a fallback
        # task, so nothing to run.
        from actor import server, AppConfig, Role
        cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        new_actor_call = AsyncMock(return_value=fake_actor)
        with patch("actor.service.RemoteActorService.new_actor", new_actor_call), \
             patch("actor.server._spawn_background_run") as spawn, \
             patch("actor.config.load_config", return_value=cfg):
            _run_async(server.new_actor(name="foo", role="qa"))
        kwargs = new_actor_call.call_args.kwargs
        self.assertEqual(kwargs["role_name"], "qa")
        spawn.assert_not_called()

    def test_new_actor_role_with_explicit_prompt_runs_with_explicit(self):
        from actor import server, AppConfig, Role
        cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        with patch("actor.service.RemoteActorService.new_actor", AsyncMock(return_value=fake_actor)), \
             patch("actor.server._spawn_background_run") as spawn, \
             patch("actor.config.load_config", return_value=cfg):
            _run_async(server.new_actor(
                name="foo", role="qa", prompt="review the auth module",
            ))
        # spawn receives the explicit task prompt, not the role's prompt.
        spawn.assert_called_once()
        spawn_args = spawn.call_args
        prompt_arg = (
            spawn_args.args[1] if len(spawn_args.args) > 1
            else spawn_args.kwargs["prompt"]
        )
        self.assertEqual(prompt_arg, "review the auth module")

    def test_new_actor_role_agent_used_when_agent_param_omitted(self):
        from actor import server, AppConfig, Role
        cfg = AppConfig(roles={
            "code": Role(name="code", agent="codex"),
        })
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        new_actor_call = AsyncMock(return_value=fake_actor)
        with patch("actor.service.RemoteActorService.new_actor", new_actor_call), \
             patch("actor.server._spawn_background_run"), \
             patch("actor.config.load_config", return_value=cfg):
            _run_async(server.new_actor(name="foo", role="code"))
        kwargs = new_actor_call.call_args.kwargs
        # `agent` arg from MCP is None; service.new_actor will resolve via role.
        self.assertIsNone(kwargs["agent_name"])
        self.assertEqual(kwargs["role_name"], "code")

    def test_run_actor_forwards_config(self):
        from actor import server
        from actor.types import AgentKind
        # server.run_actor reads the actor's agent kind to validate
        # --config; pin it to a real kind so agent_class doesn't reject
        # a MagicMock.
        actor_row = MagicMock()
        actor_row.agent = AgentKind.CLAUDE
        with patch(
            "actor.service.RemoteActorService.get_actor",
            new=AsyncMock(return_value=actor_row),
        ), patch("actor.server._spawn_background_run") as spawn:
            _run_async(server.run_actor(
                name="foo", prompt="do x", config=["model=opus"],
            ))
        args, kwargs = spawn.call_args
        overrides = kwargs["cli_overrides"]
        self.assertEqual(overrides.agent_args.get("model"), "opus")
        self.assertEqual(overrides.actor_keys, {})

    # -- use_subscription param on new_actor --------------------------------

    def _capture_new_actor_overrides(self, **kwargs) -> ActorConfig:
        """Invoke server.new_actor with no prompt, return the
        `config` kwarg the service saw."""
        from actor import server
        fake_actor = MagicMock()
        fake_actor.dir = "/tmp/foo"
        new_actor_call = AsyncMock(return_value=fake_actor)
        with patch("actor.service.RemoteActorService.new_actor", new_actor_call), \
             patch("actor.server._spawn_background_run") as spawn:
            _run_async(server.new_actor(name="foo", **kwargs))
        spawn.assert_not_called()
        return new_actor_call.call_args.kwargs["config"]

    def test_new_actor_use_subscription_true_sets_actor_key(self):
        config = self._capture_new_actor_overrides(use_subscription=True)
        self.assertEqual(config.actor_keys, {"use-subscription": "true"})
        self.assertEqual(config.agent_args, {})

    def test_new_actor_use_subscription_false_sets_actor_key(self):
        config = self._capture_new_actor_overrides(use_subscription=False)
        self.assertEqual(config.actor_keys, {"use-subscription": "false"})
        self.assertEqual(config.agent_args, {})

    def test_new_actor_use_subscription_default_none_emits_nothing(self):
        """Tri-state: omitting the param leaves actor_keys empty so
        lower layers (role / kdl / class default) supply the value."""
        config = self._capture_new_actor_overrides()
        self.assertEqual(config.actor_keys, {})

    def test_new_actor_rejects_use_subscription_via_config(self):
        from actor import server
        new_actor_call = AsyncMock()
        with patch("actor.service.RemoteActorService.new_actor", new_actor_call):
            with self.assertRaises(ConfigError):
                _run_async(server.new_actor(
                    name="foo", config=["use-subscription=true"],
                ))
        new_actor_call.assert_not_called()

    # -- use_subscription param on run_actor --------------------------------

    def _capture_run_actor_overrides(self, **kwargs) -> ActorConfig:
        from actor import server
        from actor.types import AgentKind
        actor_row = MagicMock()
        actor_row.agent = AgentKind.CLAUDE
        with patch(
            "actor.service.RemoteActorService.get_actor",
            new=AsyncMock(return_value=actor_row),
        ), patch("actor.server._spawn_background_run") as spawn:
            _run_async(server.run_actor(name="foo", prompt="do x", **kwargs))
        return spawn.call_args.kwargs["cli_overrides"]

    def test_run_actor_use_subscription_true_sets_actor_key(self):
        overrides = self._capture_run_actor_overrides(use_subscription=True)
        self.assertEqual(overrides.actor_keys, {"use-subscription": "true"})
        self.assertEqual(overrides.agent_args, {})

    def test_run_actor_use_subscription_false_sets_actor_key(self):
        overrides = self._capture_run_actor_overrides(use_subscription=False)
        self.assertEqual(overrides.actor_keys, {"use-subscription": "false"})
        self.assertEqual(overrides.agent_args, {})

    def test_run_actor_use_subscription_default_none_emits_nothing(self):
        overrides = self._capture_run_actor_overrides()
        self.assertEqual(overrides.actor_keys, {})

    def test_run_actor_rejects_use_subscription_via_config(self):
        from actor import server
        from actor.types import AgentKind
        actor_row = MagicMock()
        actor_row.agent = AgentKind.CLAUDE
        with patch(
            "actor.service.RemoteActorService.get_actor",
            new=AsyncMock(return_value=actor_row),
        ), patch("actor.server._spawn_background_run") as spawn:
            with self.assertRaises(ConfigError):
                _run_async(server.run_actor(
                    name="foo", prompt="do x", config=["use-subscription=true"],
                ))
        spawn.assert_not_called()


class AskBlockIntegrationTests(unittest.TestCase):
    """`ask { }` strings are appended to the affected MCP tool
    descriptions at server-startup time. Verified by inspecting the
    registered tool's description via FastMCP's tool list."""

    def _tool_descriptions(self):
        # Run the async list against an isolated event loop so the
        # test works in any host context.
        import asyncio
        from actor import server
        return {t.name: t.description for t in asyncio.run(server.mcp.list_tools())}

    def test_default_ask_strings_present_in_tool_descriptions(self):
        # The hardcoded defaults should already be appended (no
        # settings.kdl in the test cwd, so resolved() returns the
        # defaults — _ASK_RESOLVED captured them at module import).
        from actor.config import ASK_DEFAULTS
        descs = self._tool_descriptions()
        # Each affected tool should contain the FIRST line of its
        # default — not full equality, since the docstring base is
        # also there.
        first_line = lambda s: s.splitlines()[0]
        self.assertIn(first_line(ASK_DEFAULTS["on-start"]), descs["new_actor"])
        self.assertIn(first_line(ASK_DEFAULTS["before-run"]), descs["run_actor"])
        self.assertIn(first_line(ASK_DEFAULTS["on-discard"]), descs["discard_actor"])

    def test_unaffected_tools_have_no_ask_appendix(self):
        # `list_actors`, `show_actor`, etc. don't take ask annotations
        # — their descriptions stay as the original docstring.
        descs = self._tool_descriptions()
        # The on-start default mentions "AskUserQuestion" — make sure
        # it didn't accidentally land in tools that aren't supposed to
        # carry it.
        self.assertNotIn("AskUserQuestion", descs["list_actors"])
        self.assertNotIn("AskUserQuestion", descs["show_actor"])
        self.assertNotIn("AskUserQuestion", descs["stop_actor"])
        self.assertNotIn("AskUserQuestion", descs["list_roles"])

    def test_ask_tool_decorator_with_silenced_value_appends_nothing(self):
        # Direct unit test of the helper — given an empty appendix, no
        # blank line gets tacked onto the docstring.
        from actor import server
        from mcp.server.fastmcp import FastMCP
        local_mcp = FastMCP("test")
        original_mcp = server.mcp
        original_resolved = server._ASK_RESOLVED
        try:
            server.mcp = local_mcp
            server._ASK_RESOLVED = {"on-start": ""}

            @server._ask_tool("on-start")
            def fake_tool() -> str:
                """base docstring"""
                return "ok"

            import asyncio
            tools = {t.name: t for t in asyncio.run(local_mcp.list_tools())}
            self.assertEqual(tools["fake_tool"].description, "base docstring")
        finally:
            server.mcp = original_mcp
            server._ASK_RESOLVED = original_resolved


if __name__ == "__main__":
    unittest.main()
