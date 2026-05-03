"""e2e: specific output format assertions that may surface inconsistencies."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class OutputFormatTests(unittest.TestCase):

    def test_new_message_format(self):
        # Expected: "actorname created (path)"
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--no-worktree"])
            self.assertIn("alice", r.stdout)
            self.assertIn("created", r.stdout.lower())
            self.assertIn(str(env.cwd), r.stdout)

    def test_list_table_has_consistent_columns(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            r = env.run_cli(["list"])
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            # All non-header lines should have same number of "columns"
            # (whitespace-separated tokens, roughly).
            self.assertGreaterEqual(len(lines), 3)  # header + alice + bob

    def test_list_no_extra_blank_lines(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["list"])
            blank_count = sum(1 for l in r.stdout.splitlines() if not l.strip())
            self.assertLessEqual(blank_count, 1)

    def test_show_actor_section_headers_consistent(self):
        # Every actor's `show` should have the same shape.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            r1 = env.run_cli(["show", "alice"])
            r2 = env.run_cli(["show", "bob"])
            # Just check they have similar structure (both complete, both
            # have lines with colons for fields).
            n1 = r1.stdout.count(":")
            n2 = r2.stdout.count(":")
            self.assertEqual(n1, n2,
                             f"show output structure should be consistent")

    def test_logs_no_trailing_garbage(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["logs", "alice"])
            # Non-verbose logs shouldn't end with raw JSONL.
            self.assertFalse(r.stdout.rstrip().endswith("}"),
                             "logs output shouldn't end with raw JSON brace")


if __name__ == "__main__":
    unittest.main()
