"""e2e: codex agent path coverage."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import codex_responds
from e2e.harness.isolated_home import isolated_home


class CodexPathTests(unittest.TestCase):

    def test_codex_actor_runs_with_default_flags(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x", "--agent", "codex"],
                        **codex_responds("ok"))
            invs = env.codex_invocations()
            self.assertEqual(len(invs), 1)
            # Default actor.sh codex flags: -a never, --sandbox danger-full-access
            parsed = invs[0]["parsed"]
            self.assertEqual(parsed["approval"], "never")
            self.assertEqual(parsed["sandbox"], "danger-full-access")

    def test_codex_actor_with_model_config(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x", "--agent", "codex",
                         "--config", "m=o3"], **codex_responds("ok"))
            invs = env.codex_invocations()
            self.assertEqual(invs[0]["parsed"]["model"], "o3")

    def test_codex_actor_resumes_session(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first", "--agent", "codex"],
                        **codex_responds("a"))
            env.run_cli(["run", "alice", "second"], **codex_responds("b"))
            invs = env.codex_invocations()
            self.assertEqual(len(invs), 2)
            # actor.sh resumes codex via `codex ... exec resume <session_id>
            # <prompt>`, so argv contains "exec" as the subcommand and
            # "resume" + the session id as the next positional args.
            argv = invs[1]["argv"]
            self.assertIn("exec", argv)
            self.assertIn("resume", argv)


if __name__ == "__main__":
    unittest.main()
