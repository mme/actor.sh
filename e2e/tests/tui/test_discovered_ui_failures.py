"""e2e TUI: realistic actor-watch log-pane failures found with Pilot.

Every test below drives the current `actor watch` OVERVIEW log pane.
These are not future/spec-only features; each failure is a current
user workflow where the visible RichLog stays stale or misses newly
available log content.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, wait_for_actor_in_tree, watch_app


class DiscoveredActorWatchUiFailures(unittest.IsolatedAsyncioTestCase):

    async def _log_text(self, app) -> str:
        from textual.widgets import RichLog

        log = app.query_one("#logs-content", RichLog)
        return "\n".join(str(line) for line in getattr(log, "lines", []))

    async def _wait_for_log_marker(self, app, pilot, marker: str) -> str:
        rendered = ""
        for _ in range(60):
            await pilot.pause(0.1)
            rendered = await self._log_text(app)
            if marker in rendered:
                break
        return rendered

    async def test_logs_switching_between_actors_shows_selected_actor_log(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha", "prompt a"], **claude_responds("ALPHA_LOG"))
            env.run_cli(["new", "beta", "prompt b"], **claude_responds("BETA_LOG"))
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                await self._wait_for_log_marker(app, pilot, "ALPHA_LOG")

                await select_actor(pilot, app, "beta")
                rendered = await self._wait_for_log_marker(app, pilot, "BETA_LOG")

                self.assertIn("BETA_LOG", rendered)
                self.assertNotIn("ALPHA_LOG", rendered)

    async def test_logs_stream_appended_frames_for_selected_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha", "first"], **claude_responds("FIRST_LOG"))
            actor = env.fetch_actor("alpha")

            from actor.agents.claude import ClaudeAgent

            encoded = ClaudeAgent._encode_dir(Path(actor.dir))
            log_path = (
                env.home
                / ".claude"
                / "projects"
                / encoded
                / f"{actor.agent_session}.jsonl"
            )

            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                await self._wait_for_log_marker(app, pilot, "FIRST_LOG")
                with log_path.open("a") as f:
                    f.write(json.dumps({
                        "type": "assistant",
                        "message": {"content": [
                            {"type": "text", "text": "APPENDED_LOG"},
                        ]},
                        "timestamp": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    }) + "\n")

                rendered = await self._wait_for_log_marker(app, pilot, "APPENDED_LOG")
                self.assertIn("APPENDED_LOG", rendered)

    async def test_logs_stream_appended_user_frames_for_selected_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha", "first"], **claude_responds("FIRST_LOG"))
            actor = env.fetch_actor("alpha")

            from actor.agents.claude import ClaudeAgent

            encoded = ClaudeAgent._encode_dir(Path(actor.dir))
            log_path = (
                env.home
                / ".claude"
                / "projects"
                / encoded
                / f"{actor.agent_session}.jsonl"
            )

            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                await self._wait_for_log_marker(app, pilot, "FIRST_LOG")
                with log_path.open("a") as f:
                    f.write(json.dumps({
                        "type": "user",
                        "message": {"content": "APPENDED_USER_LOG"},
                        "timestamp": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    }) + "\n")

                rendered = await self._wait_for_log_marker(
                    app, pilot, "APPENDED_USER_LOG"
                )
                self.assertIn("APPENDED_USER_LOG", rendered)

    async def test_logs_extend_when_selected_actor_runs_again(self):
        # `actor run` resumes the actor's existing session (same
        # session_id, JSONL appended in place), so the log should
        # extend — OLD turn stays, NEW turn lands underneath. If we
        # got OLD without NEW, the renderer dropped the appended
        # frames; that's the bug to catch here.
        with isolated_home() as env:
            env.run_cli(["new", "alpha", "old"], **claude_responds("OLD_TURN"))
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                await self._wait_for_log_marker(app, pilot, "OLD_TURN")

                env.run_cli(["run", "alpha", "new"], **claude_responds("NEW_TURN"))
                rendered = await self._wait_for_log_marker(app, pilot, "NEW_TURN")

                self.assertIn("NEW_TURN", rendered)
                self.assertIn("OLD_TURN", rendered)

    async def test_logs_refresh_for_discarded_and_recreated_actor_name(self):
        # Discard preserves the git branch (default on-discard hook
        # only catches unstaged changes — destroying the branch on
        # discard would silently lose committed work). The user-side
        # recovery for reusing the name is `git branch -D <name>` in
        # the source repo; reproduce that here, then assert that the
        # log pane swaps to the new session's content (different
        # session_id → bucket popped → NEW_ALPHA replaces OLD_ALPHA).
        import subprocess
        with isolated_home() as env:
            env.run_cli(["new", "alpha", "old"], **claude_responds("OLD_ALPHA"))
            actor = env.fetch_actor("alpha")
            source_repo = actor.source_repo
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await select_actor(pilot, app, "alpha")
                await self._wait_for_log_marker(app, pilot, "OLD_ALPHA")

                env.run_cli(["discard", "alpha", "--force"])
                for _ in range(50):
                    await pilot.pause(0.1)
                    if "alpha" not in env.list_actor_names():
                        break
                subprocess.run(
                    ["git", "branch", "-D", "alpha"],
                    cwd=source_repo, check=True, capture_output=True,
                )
                env.run_cli(["new", "alpha", "new"], **claude_responds("NEW_ALPHA"))
                await wait_for_actor_in_tree(pilot, app, "alpha", timeout=8)
                await select_actor(pilot, app, "alpha")
                rendered = await self._wait_for_log_marker(app, pilot, "NEW_ALPHA")

                self.assertIn("NEW_ALPHA", rendered)
                self.assertNotIn("OLD_ALPHA", rendered)

    async def test_logs_show_background_append_when_actor_is_selected_later(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha", "first"], **claude_responds("FIRST_ALPHA"))
            env.run_cli(["new", "beta", "first"], **claude_responds("FIRST_BETA"))
            actor = env.fetch_actor("alpha")

            from actor.agents.claude import ClaudeAgent

            encoded = ClaudeAgent._encode_dir(Path(actor.dir))
            log_path = (
                env.home
                / ".claude"
                / "projects"
                / encoded
                / f"{actor.agent_session}.jsonl"
            )

            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await select_actor(pilot, app, "beta")
                await self._wait_for_log_marker(app, pilot, "FIRST_BETA")
                with log_path.open("a") as f:
                    f.write(json.dumps({
                        "type": "assistant",
                        "message": {"content": [
                            {"type": "text", "text": "ALPHA_BACKGROUND_LOG"},
                        ]},
                        "timestamp": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    }) + "\n")
                await pilot.pause(2.5)

                await select_actor(pilot, app, "alpha")
                rendered = await self._wait_for_log_marker(
                    app, pilot, "ALPHA_BACKGROUND_LOG"
                )
                self.assertIn("ALPHA_BACKGROUND_LOG", rendered)
                self.assertNotIn("FIRST_BETA", rendered)

    async def test_logs_show_background_run_when_actor_is_selected_later(self):
        with isolated_home() as env:
            env.run_cli(["new", "alpha", "old"], **claude_responds("OLD_ALPHA"))
            env.run_cli(["new", "beta", "first"], **claude_responds("FIRST_BETA"))
            async with watch_app(env, size=(100, 30)) as (app, pilot):
                await select_actor(pilot, app, "beta")
                await self._wait_for_log_marker(app, pilot, "FIRST_BETA")

                env.run_cli(["run", "alpha", "new"], **claude_responds("NEW_ALPHA"))
                await pilot.pause(2.5)

                await select_actor(pilot, app, "alpha")
                rendered = await self._wait_for_log_marker(app, pilot, "NEW_ALPHA")
                self.assertIn("NEW_ALPHA", rendered)
                self.assertNotIn("FIRST_BETA", rendered)


if __name__ == "__main__":
    unittest.main()
