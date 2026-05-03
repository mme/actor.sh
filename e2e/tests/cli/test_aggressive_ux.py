"""e2e CLI: aggressive UX assertions guaranteed to find polish gaps."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class AggressiveUxTests(unittest.TestCase):

    def test_show_includes_role_information_when_role_was_applied(self):
        # If an actor was created via --role X, show should mention X.
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    description "QA"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            r = env.run_cli(["show", "alice"])
            self.assertIn("qa", r.stdout)

    def test_list_shows_running_status_with_count(self):
        # If 2 actors are running and 1 is idle, list output should
        # reflect that mix.
        import subprocess, time
        with isolated_home() as env:
            ps = []
            for n in ("a1", "a2"):
                ps.append(subprocess.Popen(
                    ["actor", "new", n, "long task"],
                    env=env.env(**claude_responds("ok", sleep=2)),
                    cwd=str(env.cwd),
                ))
            try:
                env.run_cli(["new", "a3"])  # idle
                time.sleep(0.5)
                r = env.run_cli(["list"])
                self.assertEqual(r.returncode, 0, msg=r.stderr)
                running_count = r.stdout.lower().count("running")
                idle_count = r.stdout.lower().count("idle")
                self.assertGreaterEqual(running_count, 2,
                                        f"expected ≥2 running, got: {r.stdout}")
                self.assertGreaterEqual(idle_count, 1,
                                        f"expected ≥1 idle, got: {r.stdout}")
            finally:
                for p in ps:
                    if p.poll() is None:
                        p.kill()
                        p.wait(timeout=5)

    def test_logs_command_handles_concurrent_run(self):
        # Logs while another run is in flight shouldn't crash.
        import subprocess, time
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            p = subprocess.Popen(
                ["actor", "run", "alice", "long task"],
                env=env.env(**claude_responds("ok", sleep=2)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                r = env.run_cli(["logs", "alice"])
                p.wait(timeout=10)
                self.assertEqual(r.returncode, 0, msg=r.stderr)
            finally:
                if p.poll() is None:
                    p.kill()

    def test_codex_invocation_includes_cd_flag(self):
        # actor.sh's codex agent passes -C <dir> for the cwd.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x", "--agent", "codex"],
                        **codex_responds("ok"))
            invs = env.codex_invocations()
            argv = invs[0]["argv"]
            self.assertIn("-C", argv,
                          f"codex should get -C cwd flag; got {argv}")

    def test_actor_main_message_when_no_args(self):
        # actor main with no extra args = orchestrator session boot.
        # The exec'd claude should get just channel + prompt.
        import subprocess
        with isolated_home() as env:
            r = subprocess.run(
                ["actor", "main"],
                env=env.env(**claude_responds("hello", exit=0)),
                cwd=str(env.cwd),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            argv = invs[0]["argv"]
            # No -p flag (interactive mode).
            self.assertNotIn("-p", argv)


if __name__ == "__main__":
    unittest.main()
