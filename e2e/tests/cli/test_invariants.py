"""e2e: code-path invariants — things that should always be true."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class CrossInvariantTests(unittest.TestCase):

    def test_claude_actor_always_gets_channel_flag(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertTrue(invs[0]["parsed"]["channel_flag"],
                            "claude actor should always get the channel flag")

    def test_claude_actor_gets_print_mode_flag(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertTrue(invs[0]["parsed"]["print_mode"],
                            "non-interactive claude actor needs -p")

    def test_claude_actor_gets_session_id_on_first_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertIsNotNone(invs[0]["parsed"]["session_id"])

    def test_actor_name_appears_in_actor_dir_path(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertIn("alice", actor.dir)

    def test_no_subcommand_after_codex_in_argv_when_resuming(self):
        # When resuming codex via `codex exec resume <id>`, the
        # subcommand string is "exec" (not "resume"), but resume must
        # be present as a positional.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first", "--agent", "codex"],
                        **codex_responds("a"))
            env.run_cli(["run", "alice", "second"], **codex_responds("b"))
            invs = env.codex_invocations()
            self.assertEqual(len(invs), 2)
            argv = invs[1]["argv"]
            self.assertIn("exec", argv)


if __name__ == "__main__":
    unittest.main()
