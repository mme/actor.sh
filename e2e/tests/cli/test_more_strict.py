"""e2e: more strict invariant + behavioral assertions."""
from __future__ import annotations

import unittest
from pathlib import Path

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreStrictTests(unittest.TestCase):

    def test_after_run_hook_receives_exit_code_zero(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_EXIT_CODE > $HOME/exitcode.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            self.assertEqual(
                (env.home / "exitcode.txt").read_text().strip(), "0"
            )

    def test_after_run_hook_receives_exit_code_nonzero(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_EXIT_CODE > $HOME/exitcode.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("oops", exit=5))
            self.assertEqual(
                (env.home / "exitcode.txt").read_text().strip(), "5"
            )

    def test_after_run_hook_receives_run_id(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_RUN_ID > $HOME/runid.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            text = (env.home / "runid.txt").read_text().strip()
            # ACTOR_RUN_ID should be a positive integer.
            self.assertGreater(int(text), 0)

    def test_after_run_hook_receives_duration_ms(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_DURATION_MS > $HOME/dur.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("ok", sleep=0.1))
            text = (env.home / "dur.txt").read_text().strip()
            # Should be a number ≥ 100ms.
            self.assertGreaterEqual(int(text), 100)

    def test_actor_dir_in_actor_dir_env_var(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-start "echo $ACTOR_DIR > $HOME/dir.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                (env.home / "dir.txt").read_text().strip(), actor.dir
            )

    def test_actor_agent_in_actor_agent_env_var(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-start "echo $ACTOR_AGENT > $HOME/agent.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--agent", "codex"])
            self.assertEqual(
                (env.home / "agent.txt").read_text().strip(), "codex"
            )


if __name__ == "__main__":
    unittest.main()
