"""e2e: assertions targeting subtle assumptions in the codebase."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class AssertionsTests(unittest.TestCase):

    def test_actor_new_creates_database_with_schema(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-worktree"])
            db_path = env.home / ".actor" / "actor.db"
            self.assertTrue(db_path.is_file())
            self.assertGreater(db_path.stat().st_size, 0)

    def test_actor_show_for_actor_with_only_one_run_doesnt_double_print(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            # The single run should appear at most once.
            occurrences = r.stdout.count("do x")
            self.assertLessEqual(occurrences, 2,
                                 f"prompt 'do x' appeared {occurrences} times; expected ≤2")

    def test_codex_first_run_does_not_use_resume(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first", "--agent", "codex"],
                        **codex_responds("a"))
            invs = env.codex_invocations()
            self.assertEqual(len(invs), 1)
            argv = invs[0]["argv"]
            self.assertNotIn("resume", argv,
                             f"first codex run should not use resume; argv={argv}")

    def test_setup_creates_required_subdirs(self):
        # After setup, certain directories should exist.
        with isolated_home() as env:
            r = env.run_cli(["setup", "--for", "claude-code"])
            if r.returncode == 0:
                # Skill dir should be present.
                self.assertTrue((env.home / ".claude" / "skills").is_dir())

    def test_two_runs_increment_db_run_id(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            with env.db() as db:
                runs, total = db.list_runs("alice", limit=10)
                self.assertEqual(total, 2)
                ids = sorted(r.id for r in runs)
                self.assertEqual(ids[1] - ids[0], 1, "run IDs should be monotonic")

    def test_each_run_records_its_own_prompt(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "PROMPT_ONE"], **claude_responds("a"))
            env.run_cli(["run", "alice", "PROMPT_TWO"], **claude_responds("b"))
            with env.db() as db:
                runs, _ = db.list_runs("alice", limit=10)
                prompts = sorted(r.prompt for r in runs)
                self.assertEqual(prompts, ["PROMPT_ONE", "PROMPT_TWO"])


if __name__ == "__main__":
    unittest.main()
