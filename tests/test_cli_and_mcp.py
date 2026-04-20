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

from actor import __version__
from actor.cli import main
from actor.errors import ActorError


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
        self.assertEqual(kwargs["config_pairs"], [])  # saved as defaults already
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

    def test_new_translates_model_and_strip_api_keys_to_config_pairs(self):
        cmd_new, _cmd_run, _code = self._run([
            "new", "foo", "--model", "sonnet", "--no-strip-api-keys",
        ])
        pairs = cmd_new.call_args.kwargs["config_pairs"]
        self.assertIn("model=sonnet", pairs)
        self.assertIn("strip-api-keys=false", pairs)

    def test_new_passes_template_arg_to_cmd_new_and_uses_template_prompt(self):
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/actor"
        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")

        from actor import AppConfig, Template
        fake_app_cfg = AppConfig(templates={
            "qa": Template(name="qa", agent="claude", prompt="run tests"),
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
                main(["new", "foo", "--template", "qa"])
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        cmd_new.assert_called_once()
        kwargs = cmd_new.call_args.kwargs
        self.assertEqual(kwargs["template_name"], "qa")
        self.assertIsNotNone(kwargs["app_config"])
        self.assertEqual(code, 0)
        cmd_run.assert_called_once()
        self.assertEqual(cmd_run.call_args.kwargs["prompt"], "run tests")

    def test_new_cli_prompt_beats_template_prompt(self):
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/actor"
        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(return_value="done")

        from actor import AppConfig, Template
        fake_app_cfg = AppConfig(templates={
            "qa": Template(name="qa", agent="claude", prompt="template says run tests"),
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
            main(["new", "foo", "custom prompt", "--template", "qa"])
        self.assertEqual(cmd_run.call_args.kwargs["prompt"], "custom prompt")

    def test_new_without_template_does_not_pass_template_kwargs(self):
        """Regression check: normal `actor new foo` must still work end-to-end."""
        cmd_new, cmd_run, code = self._run(["new", "foo"])
        kwargs = cmd_new.call_args.kwargs
        self.assertIsNone(kwargs["template_name"])
        self.assertEqual(code, 0)

    def test_new_with_prompt_run_failure_surfaces_partial_success(self):
        fake_db = MagicMock()
        fake_actor = MagicMock()
        fake_actor.name = "foo"
        fake_actor.dir = "/tmp/foo"

        cmd_new = MagicMock(return_value=fake_actor)
        cmd_run = MagicMock(side_effect=ActorError("agent binary missing"))

        with patch("actor.cli.cmd_new", cmd_new), \
             patch("actor.cli.cmd_run", cmd_run), \
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
        cmd_run = MagicMock(return_value="ok")
        fake_db = MagicMock()
        with patch("actor.cli.cmd_run", cmd_run), \
             patch("actor.cli.Database") as db_cls, \
             patch("actor.cli._create_agent", return_value=MagicMock()):
            db_cls.open.return_value = fake_db
            stdin = io.StringIO("")
            stdin.isatty = lambda: True  # type: ignore[assignment]
            with patch("sys.stdin", stdin):
                main(["run", "foo", "--config", "model=opus", "do x"])
        cmd_run.assert_called_once()
        self.assertEqual(cmd_run.call_args.kwargs["config_pairs"], ["model=opus"])

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


class ClaudeWrapperTests(unittest.TestCase):
    def test_execs_claude_with_channel_flag(self):
        with patch("os.execvp") as execvp:
            main(["claude"])
        args, _ = execvp.call_args
        self.assertEqual(args[0], "claude")
        self.assertEqual(
            args[1],
            ["claude", "--dangerously-load-development-channels", "server:actor"],
        )

    def test_forwards_trailing_args_verbatim(self):
        with patch("os.execvp") as execvp:
            main(["claude", "--model", "opus", "fix the nav"])
        args, _ = execvp.call_args
        self.assertEqual(
            args[1],
            [
                "claude",
                "--dangerously-load-development-channels",
                "server:actor",
                "--model",
                "opus",
                "fix the nav",
            ],
        )

    def test_missing_claude_binary_exits_cleanly(self):
        with patch("os.execvp", side_effect=FileNotFoundError), \
             patch("sys.stderr", io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                main(["claude"])
            self.assertEqual(ctx.exception.code, 1)
        self.assertIn("claude", err.getvalue().lower())


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
            agent.start(Path("/tmp"), "hi", {})

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
            agent.resume(Path("/tmp"), "some-session", "continue", {})

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

    def test_run_actor_forwards_config(self):
        from actor import server
        with patch("actor.server._spawn_background_run") as spawn:
            server.run_actor(name="foo", prompt="do x", config=["model=opus"])
        args, kwargs = spawn.call_args
        self.assertEqual(kwargs["config_pairs"], ["model=opus"])


if __name__ == "__main__":
    unittest.main()
