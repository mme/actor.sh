"""e2e CLI: strict UX assertions targeting subtle inconsistencies."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class StrictUxTests(unittest.TestCase):

    def test_codex_invocation_has_expected_first_subcommand(self):
        # codex always uses `exec` for first run.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x", "--agent", "codex"],
                        **codex_responds("ok"))
            invs = env.codex_invocations()
            argv = invs[0]["argv"]
            self.assertIn("exec", argv,
                          f"first codex run should use exec; got {argv}")

    def test_actor_run_returncode_for_failure(self):
        # When the underlying agent exits nonzero, `actor run` should
        # propagate the failure (returncode != 0).
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "do x"],
                            **claude_responds("oops", exit=2))
            self.assertNotEqual(r.returncode, 0,
                                "actor run should report failure")

    def test_actor_new_with_failing_run_returncode(self):
        # `actor new alice "task"` where task fails — the actor should
        # be created, but the overall command should reflect the failure.
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "do x"],
                            **claude_responds("oops", exit=2))
            self.assertEqual(env.list_actor_names(), ["alice"])
            # The exit code should reflect the failed run.
            self.assertNotEqual(r.returncode, 0)

    def test_show_handles_actor_with_no_session_yet(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            # No session id yet — show shouldn't crash trying to display None.
            self.assertNotIn("None", r.stdout.splitlines()[0:3] or [""])

    def test_show_doesnt_show_none_for_unset_optional_fields(self):
        # Optional fields like base_branch, source_repo should display
        # gracefully when None — not literal "None".
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-worktree"])
            r = env.run_cli(["show", "alice"])
            self.assertNotIn("None", r.stdout)


if __name__ == "__main__":
    unittest.main()
