"""M1 smoke tests — prove the harness + fakes are wired correctly.

These are the foundation. If these break, every other e2e test breaks
in mysterious ways."""
from __future__ import annotations

import shutil
import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class HarnessSmokeTests(unittest.TestCase):

    def test_isolated_home_creates_dirs(self):
        with isolated_home() as env:
            self.assertTrue((env.home / ".actor").is_dir())
            self.assertTrue((env.home / ".claude" / "projects").is_dir())
            self.assertTrue(env.cwd.is_dir())
            self.assertTrue((env.cwd / ".git").is_dir())

    def test_fakes_resolve_first_on_path(self):
        with isolated_home() as env:
            path = env.env()["PATH"]
            resolved = shutil.which("claude", path=path)
            self.assertIsNotNone(resolved)
            self.assertIn("e2e/fakes/bin/claude", resolved)

    def test_actor_version_runs_in_isolated_env(self):
        with isolated_home() as env:
            r = env.run_cli(["--version"])
            self.assertEqual(r.returncode, 0)
            self.assertIn("actor-sh", r.stdout)

    def test_fake_claude_records_invocation(self):
        with isolated_home() as env:
            # Invoke fake claude directly via subprocess to prove the
            # logging mechanism works before any actor.sh integration.
            import subprocess
            r = subprocess.run(
                ["claude", "-p", "--session-id", "abc",
                 "--append-system-prompt", "you are a test", "hello"],
                env=env.env(**claude_responds(text="HI")),
                cwd=str(env.cwd),
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0)
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 1)
            parsed = invs[0]["parsed"]
            self.assertEqual(parsed["session_id"], "abc")
            self.assertEqual(parsed["append_system_prompt"], "you are a test")
            self.assertEqual(parsed["prompt"], "hello")
            self.assertTrue(parsed["print_mode"])

    def test_fake_claude_writes_session_log(self):
        with isolated_home() as env:
            import subprocess
            subprocess.run(
                ["claude", "-p", "--session-id", "xyz", "--", "what"],
                env=env.env(**claude_responds(text="answer")),
                cwd=str(env.cwd),
                capture_output=True, text=True,
            )
            # Find the log file
            projects = env.home / ".claude" / "projects"
            logs = list(projects.rglob("xyz.jsonl"))
            self.assertEqual(len(logs), 1, f"expected one log; got {logs}")
            text = logs[0].read_text()
            self.assertIn("answer", text)
            self.assertIn("what", text)

    def test_fake_codex_records_invocation(self):
        with isolated_home() as env:
            import subprocess
            subprocess.run(
                ["codex", "exec", "-m", "o3", "--sandbox", "read-only", "do thing"],
                env=env.env(**codex_responds(text="ok")),
                cwd=str(env.cwd), capture_output=True, text=True,
            )
            invs = env.codex_invocations()
            self.assertEqual(len(invs), 1)
            parsed = invs[0]["parsed"]
            self.assertEqual(parsed["subcommand"], "exec")
            self.assertEqual(parsed["model"], "o3")
            self.assertEqual(parsed["sandbox"], "read-only")
            self.assertEqual(parsed["prompt"], "do thing")

    def test_settings_kdl_user_scope(self):
        with isolated_home() as env:
            env.write_settings_kdl('role "qa" {\n    agent "claude"\n}\n')
            self.assertTrue((env.home / ".actor" / "settings.kdl").is_file())

    def test_settings_kdl_project_scope(self):
        with isolated_home() as env:
            env.write_settings_kdl('hooks { on-start "echo hi" }', scope="project")
            self.assertTrue((env.cwd / ".actor" / "settings.kdl").is_file())


if __name__ == "__main__":
    unittest.main()
