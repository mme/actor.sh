"""Tests for the CLI dispatch and MCP tool wrappers.

These test the thin glue layer on top of cmd_* — argument translation,
prompt resolution (arg vs. stdin), error handling, and the MCP tool
signatures. The cmd_* layer itself is covered by test_actor.py.
"""
from __future__ import annotations

import io
import sys
import unittest
from unittest.mock import MagicMock, patch

from actor import ActorConfig, __version__
from actor.cli import main
from actor.errors import ActorError, ConfigError


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


class NewCommandTests(unittest.TestCase):
    """`actor new` CLI dispatch."""

    def _run(self, argv, stdin_text=None, stdin_is_tty=True):
        """Invoke cli.main with argv, patching cmd_new/cmd_run/stdin/Database."""
        from actor import AppConfig
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = argv[1] if len(argv) > 1 else "a"
        fake_actor.dir = "/tmp/actor"
        fake_agent = MagicMock()

        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")
        agent_for = MagicMock(return_value=fake_agent)

        stdin = io.StringIO(stdin_text or "")
        stdin.isatty = lambda: stdin_is_tty  # type: ignore[assignment]

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.config.load_config", return_value=AppConfig()), \
             patch("actor.cli.Database") as db_cls, \
             patch("sys.stdin", stdin):
            db_cls.open.return_value = fake_db
            # Patch agent_for inline — it's a closure in cli.main, but the
            # database+_create_agent path is replaced by mocking cmd_run, so
            # agent_for is only reached via _create_agent(actor.agent).
            with patch("actor.cli._create_agent", return_value=fake_agent):
                try:
                    main(argv)
                    exit_code = 0
                except SystemExit as e:
                    exit_code = e.code if isinstance(e.code, int) else 1
        return cmd_new, cmd_run, exit_code

    def test_new_without_prompt_creates_only(self):
        cmd_new, cmd_run, code = self._run(["new", "foo"])
        cmd_new.assert_called_once()
        cmd_run.assert_not_called()
        self.assertEqual(code, 0)

    def test_new_with_prompt_arg_creates_and_runs(self):
        cmd_new, cmd_run, code = self._run(["new", "foo", "do x"])
        cmd_new.assert_called_once()
        cmd_run.assert_called_once()
        kwargs = cmd_run.call_args.kwargs
        self.assertEqual(kwargs["name"], "foo")
        self.assertEqual(kwargs["prompt"], "do x")
        self.assertEqual(kwargs["cli_overrides"], ActorConfig())  # saved as defaults already
        self.assertEqual(code, 0)

    def test_new_with_stdin_prompt_creates_and_runs(self):
        cmd_new, cmd_run, code = self._run(["new", "foo"], stdin_text="fix it\n", stdin_is_tty=False)
        cmd_new.assert_called_once()
        cmd_run.assert_called_once()
        self.assertEqual(cmd_run.call_args.kwargs["prompt"], "fix it")
        self.assertEqual(code, 0)

    def test_new_with_empty_stdin_errors(self):
        cmd_new, cmd_run, code = self._run(["new", "foo"], stdin_text="", stdin_is_tty=False)
        cmd_new.assert_called_once()
        cmd_run.assert_not_called()
        self.assertEqual(code, 1)

    def test_new_translates_model_and_use_subscription_to_cli_overrides(self):
        cmd_new, _cmd_run, _code = self._run([
            "new", "foo", "--model", "sonnet", "--no-use-subscription",
        ])
        overrides = cmd_new.call_args.kwargs["cli_overrides"]
        self.assertEqual(overrides.agent_args.get("model"), "sonnet")
        self.assertEqual(overrides.actor_keys.get("use-subscription"), "false")

    def test_new_without_use_subscription_flag_does_not_emit_override(self):
        """Tri-state check: omitting both --use-subscription and --no-use-subscription
        must NOT push a use-subscription value, so a role's value wins."""
        cmd_new, _cmd_run, _code = self._run(["new", "foo"])
        overrides = cmd_new.call_args.kwargs["cli_overrides"]
        self.assertNotIn(
            "use-subscription", overrides.actor_keys,
            f"expected no use-subscription override, got {overrides.actor_keys}",
        )

    def test_new_explicit_use_subscription_flag_emits_true_override(self):
        """Explicit --use-subscription must push use-subscription=true so it
        can override a role's use-subscription "false"."""
        cmd_new, _cmd_run, _code = self._run(["new", "foo", "--use-subscription"])
        overrides = cmd_new.call_args.kwargs["cli_overrides"]
        self.assertEqual(overrides.actor_keys.get("use-subscription"), "true")

    def test_new_passes_role_arg_to_cmd_new(self):
        # `actor new foo --role qa` (no prompt) creates the actor with the
        # role applied but does not auto-run — the role's `prompt` is the
        # actor's system prompt, not a fallback task.
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/actor"
        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")

        from actor import AppConfig, Role
        fake_app_cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })

        stdin = io.StringIO("")
        stdin.isatty = lambda: True  # type: ignore[assignment]

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.config.load_config", return_value=fake_app_cfg), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()), \
             patch("sys.stdin", stdin):
            db_cls.open.return_value = fake_db
            try:
                main(["new", "foo", "--role", "qa"])
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        cmd_new.assert_called_once()
        kwargs = cmd_new.call_args.kwargs
        self.assertEqual(kwargs["role_name"], "qa")
        self.assertIsNotNone(kwargs["app_config"])
        self.assertEqual(code, 0)
        # No auto-run: a role's prompt is its identity, not a default task.
        cmd_run.assert_not_called()

    def test_new_with_role_and_explicit_prompt_runs_with_explicit_prompt(self):
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/actor"
        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")

        from actor import AppConfig, Role
        fake_app_cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })

        stdin = io.StringIO("")
        stdin.isatty = lambda: True  # type: ignore[assignment]

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.config.load_config", return_value=fake_app_cfg), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()), \
             patch("sys.stdin", stdin):
            db_cls.open.return_value = fake_db
            main(["new", "foo", "review the auth module", "--role", "qa"])
        self.assertEqual(cmd_run.call_args.kwargs["prompt"], "review the auth module")

    def test_new_without_role_does_not_pass_role_kwargs(self):
        """Regression check: normal `actor new foo` must still work end-to-end."""
        cmd_new, cmd_run, code = self._run(["new", "foo"])
        kwargs = cmd_new.call_args.kwargs
        self.assertIsNone(kwargs["role_name"])
        self.assertEqual(code, 0)

    def test_new_empty_stdin_with_role_does_not_run(self):
        """`echo "" | actor new foo --role qa` creates the actor without
        auto-running — empty stdin no longer falls back to the role's
        prompt (which is now a system prompt, not a task)."""
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/actor"
        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")

        from actor import AppConfig, Role
        fake_app_cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })

        stdin = io.StringIO("")
        stdin.isatty = lambda: False  # type: ignore[assignment]

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.config.load_config", return_value=fake_app_cfg), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()), \
             patch("sys.stdin", stdin), \
             patch("sys.stderr", io.StringIO()):
            db_cls.open.return_value = fake_db
            with self.assertRaises(SystemExit) as ctx:
                main(["new", "foo", "--role", "qa"])
            self.assertEqual(ctx.exception.code, 1)
        cmd_run.assert_not_called()

    def test_new_surfaces_config_error_from_load_config(self):
        """A malformed settings.kdl must exit non-zero with the error text
        on stderr, not crash with an uncaught ConfigError."""
        from actor.errors import ConfigError
        fake_db = MagicMock()
        with patch("actor.config.load_config",
                   side_effect=ConfigError("parse error in /x/settings.kdl: boom")), \
             patch("actor.cli.Database") as db_cls:
            db_cls.open.return_value = fake_db
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
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/foo"

        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(side_effect=ActorError("agent binary missing"))

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.config.load_config", return_value=AppConfig()), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()):
            db_cls.open.return_value = fake_db
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
        from actor.types import AgentKind
        cmd_run = MagicMock(return_value="ok")
        fake_db = MagicMock()
        # cli.main resolves the actor's agent kind before building cli_overrides;
        # pin it so _agent_class sees a valid kind (not a MagicMock attribute).
        fake_db.get_actor.return_value.agent = AgentKind.CLAUDE
        with patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()):
            db_cls.open.return_value = fake_db
            stdin = io.StringIO("")
            stdin.isatty = lambda: True  # type: ignore[assignment]
            with patch("sys.stdin", stdin):
                main(["run", "foo", "--config", "model=opus", "do x"])
        cmd_run.assert_called_once()
        overrides = cmd_run.call_args.kwargs["cli_overrides"]
        self.assertEqual(overrides.agent_args.get("model"), "opus")
        self.assertEqual(overrides.actor_keys, {})

    def test_run_dash_i_dispatches_to_cmd_interactive(self):
        """`actor run foo -i` must route through cmd_interactive, not cmd_run."""
        cmd_interactive = MagicMock(return_value=(0, "ok"))
        cmd_run = MagicMock(return_value="should-not-run")
        fake_db = MagicMock()
        with patch("actor.cli.cmd_interactive", cmd_interactive), \
             patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()):
            db_cls.open.return_value = fake_db
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main(["run", "foo", "-i"])
        self.assertEqual(ctx.exception.code, 0)
        cmd_interactive.assert_called_once()
        self.assertEqual(cmd_interactive.call_args.kwargs.get("name"), "foo")
        cmd_run.assert_not_called()

    def test_run_dash_i_maps_signal_to_posix_exit(self):
        """Negative exit code from cmd_interactive (signal) → 128 + signum."""
        import signal as _sig
        cmd_interactive = MagicMock(return_value=(-_sig.SIGTERM, "stopped"))
        fake_db = MagicMock()
        with patch("actor.cli.cmd_interactive", cmd_interactive), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()):
            db_cls.open.return_value = fake_db
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main(["run", "foo", "-i"])
        self.assertEqual(ctx.exception.code, 128 + _sig.SIGTERM)

    def test_run_without_prompt_and_tty_exits_nonzero(self):
        fake_db = MagicMock()
        with patch("actor.cli.Database") as db_cls:
            db_cls.open.return_value = fake_db
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
        # System prompt is appended via the --append-system-prompt flag so it
        # layers on top of Claude Code's defaults instead of replacing them.
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
        # `actor main` is claude-only today; if the user overrode main to
        # use codex, fail loudly rather than silently doing the wrong thing.
        with self._patch_main_role(agent="codex"), \
             patch("os.execvp") as execvp, \
             patch("sys.stderr", io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                main(["main"])
            self.assertEqual(ctx.exception.code, 1)
        execvp.assert_not_called()
        self.assertIn("codex", err.getvalue())

    def test_role_without_prompt_omits_append_flag(self):
        # A `main` role with no prompt set still launches the orchestrator
        # session — just without the system-prompt append.
        with self._patch_main_role(prompt=None), patch("os.execvp") as execvp:
            main(["main"])
        argv = execvp.call_args.args[1]
        self.assertNotIn("--append-system-prompt", argv)

    def test_prompt_passed_verbatim_no_shell_escaping(self):
        # execvp passes argv entries directly to the child process — no shell,
        # no quoting, no interpolation. Tricky characters that would need
        # escaping in a shell command (newlines, quotes, $, backticks,
        # backslashes) must reach the child verbatim as one argv entry.
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
    """ClaudeAgent must launch sub-claudes with the channel flag so nested actors
    receive completion notifications identically to the top-level session."""

    def test_start_includes_channel_flag(self):
        from actor.agents.claude import ClaudeAgent
        from pathlib import Path
        agent = ClaudeAgent()

        captured = {}

        def fake_spawn(self, args, cwd, config):
            captured["args"] = args
            return 12345

        with patch.object(ClaudeAgent, "_spawn_and_track", fake_spawn):
            agent.start(Path("/tmp"), "hi", ActorConfig())

        self.assertIn("--dangerously-load-development-channels", captured["args"])
        idx = captured["args"].index("--dangerously-load-development-channels")
        self.assertEqual(captured["args"][idx + 1], "server:actor")

    def test_resume_includes_channel_flag(self):
        from actor.agents.claude import ClaudeAgent
        from pathlib import Path
        agent = ClaudeAgent()

        captured = {}

        def fake_spawn(self, args, cwd, config):
            captured["args"] = args
            return 12345

        with patch.object(ClaudeAgent, "_spawn_and_track", fake_spawn):
            agent.resume(Path("/tmp"), "some-session", "continue", ActorConfig())

        self.assertIn("--dangerously-load-development-channels", captured["args"])


class McpToolTests(unittest.TestCase):
    """Exercise new_actor / run_actor wrappers."""

    def test_run_actor_strips_and_rejects_whitespace_prompt(self):
        from actor.server import run_actor
        with self.assertRaises(ActorError):
            run_actor(name="foo", prompt="   ")

    def test_new_actor_without_prompt_does_not_spawn(self):
        from actor import server
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run") as spawn:
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            msg = server.new_actor(name="foo")
        spawn.assert_not_called()
        self.assertIn("created", msg)
        self.assertNotIn("running", msg)

    def test_new_actor_with_whitespace_prompt_does_not_spawn(self):
        from actor import server
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run") as spawn:
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            msg = server.new_actor(name="foo", prompt="   ")
        spawn.assert_not_called()
        self.assertIn("created", msg)
        self.assertNotIn("running", msg)

    def test_new_actor_with_prompt_spawns_and_reports(self):
        from actor import server
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run") as spawn:
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            msg = server.new_actor(name="foo", prompt="do x")
        spawn.assert_called_once()
        self.assertIn("running", msg)

    def test_new_actor_spawn_failure_reports_partial_success(self):
        from actor import server
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run", side_effect=RuntimeError("boom")):
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            with patch("sys.stderr", io.StringIO()):
                msg = server.new_actor(name="foo", prompt="do x")
        self.assertIn("created", msg)
        self.assertIn("run failed to start", msg)

    def test_new_actor_passes_role_to_cmd_new_no_auto_run(self):
        # `new_actor(role="qa")` with no prompt creates an idle actor —
        # the role's `prompt` is its system prompt, not a fallback task,
        # so nothing to run.
        from actor import server, AppConfig, Role
        cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run") as spawn, \
             patch("actor.config.load_config", return_value=cfg):
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            server.new_actor(name="foo", role="qa")
        kwargs = cmd_new.call_args.kwargs
        self.assertEqual(kwargs["role_name"], "qa")
        spawn.assert_not_called()

    def test_new_actor_role_with_explicit_prompt_runs_with_explicit(self):
        from actor import server, AppConfig, Role
        cfg = AppConfig(roles={
            "qa": Role(name="qa", agent="claude", prompt="You are a QA engineer."),
        })
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run") as spawn, \
             patch("actor.config.load_config", return_value=cfg):
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            server.new_actor(name="foo", role="qa", prompt="review the auth module")
        # spawn receives the explicit task prompt, not the role's prompt.
        spawn.assert_called_once()
        spawn_args = spawn.call_args
        prompt_arg = spawn_args.args[1] if len(spawn_args.args) > 1 else spawn_args.kwargs["prompt"]
        self.assertEqual(prompt_arg, "review the auth module")

    def test_new_actor_role_agent_used_when_agent_param_omitted(self):
        from actor import server, AppConfig, Role
        cfg = AppConfig(roles={
            "code": Role(name="code", agent="codex"),
        })
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run") as spawn, \
             patch("actor.config.load_config", return_value=cfg):
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            server.new_actor(name="foo", role="code")
        kwargs = cmd_new.call_args.kwargs
        # `agent` arg from MCP is None; cmd_new will resolve via role.
        self.assertIsNone(kwargs["agent_name"])
        self.assertEqual(kwargs["role_name"], "code")

    def test_run_actor_forwards_config(self):
        from actor import server
        from actor.types import AgentKind
        # server.run_actor reads the actor's agent kind to validate --config;
        # pin it to a real kind so _agent_class doesn't reject a MagicMock.
        with patch("actor.server._db") as db_fn, \
             patch("actor.server._spawn_background_run") as spawn:
            db_fn.return_value.get_actor.return_value.agent = AgentKind.CLAUDE
            server.run_actor(name="foo", prompt="do x", config=["model=opus"])
        args, kwargs = spawn.call_args
        overrides = kwargs["cli_overrides"]
        self.assertEqual(overrides.agent_args.get("model"), "opus")
        self.assertEqual(overrides.actor_keys, {})

    # -- use_subscription param on new_actor ----------------------------------

    def _capture_new_actor_overrides(self, **kwargs) -> ActorConfig:
        """Invoke server.new_actor with no prompt, return cli_overrides cmd_new saw."""
        from actor import server
        with patch("actor.server.cmd_new") as cmd_new, \
             patch("actor.server._spawn_background_run") as spawn:
            fake_actor = MagicMock()
            fake_actor.dir = "/tmp/foo"
            cmd_new.return_value = fake_actor
            server.new_actor(name="foo", **kwargs)
        spawn.assert_not_called()
        return cmd_new.call_args.kwargs["cli_overrides"]

    def test_new_actor_use_subscription_true_sets_actor_key(self):
        overrides = self._capture_new_actor_overrides(use_subscription=True)
        self.assertEqual(overrides.actor_keys, {"use-subscription": "true"})
        self.assertEqual(overrides.agent_args, {})

    def test_new_actor_use_subscription_false_sets_actor_key(self):
        overrides = self._capture_new_actor_overrides(use_subscription=False)
        self.assertEqual(overrides.actor_keys, {"use-subscription": "false"})
        self.assertEqual(overrides.agent_args, {})

    def test_new_actor_use_subscription_default_none_emits_nothing(self):
        """Tri-state: omitting the param leaves actor_keys empty so lower
        layers (role / kdl / class default) supply the value."""
        overrides = self._capture_new_actor_overrides()
        self.assertEqual(overrides.actor_keys, {})

    def test_new_actor_rejects_use_subscription_via_config(self):
        from actor import server
        with patch("actor.server.cmd_new") as cmd_new:
            with self.assertRaises(ConfigError):
                server.new_actor(name="foo", config=["use-subscription=true"])
        cmd_new.assert_not_called()

    # -- use_subscription param on run_actor ----------------------------------

    def _capture_run_actor_overrides(self, **kwargs) -> ActorConfig:
        from actor import server
        from actor.types import AgentKind
        with patch("actor.server._db") as db_fn, \
             patch("actor.server._spawn_background_run") as spawn:
            db_fn.return_value.get_actor.return_value.agent = AgentKind.CLAUDE
            server.run_actor(name="foo", prompt="do x", **kwargs)
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
        with patch("actor.server._db") as db_fn, \
             patch("actor.server._spawn_background_run") as spawn:
            db_fn.return_value.get_actor.return_value.agent = AgentKind.CLAUDE
            with self.assertRaises(ConfigError):
                server.run_actor(
                    name="foo", prompt="do x", config=["use-subscription=true"],
                )
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
