"""e2e CLI: more strict UX assertions."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreStrictUxTests(unittest.TestCase):

    def test_show_separates_runs_section_from_metadata(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            # There should be a clear visual separator (e.g. blank
            # line, header, dashes) between metadata and runs.
            lines = r.stdout.splitlines()
            has_separator = any(
                set(line.strip()) <= set("-=─━") and len(line.strip()) > 3
                for line in lines
            ) or any(line.strip() == "" for line in lines)
            self.assertTrue(has_separator, f"show should have a section separator: {r.stdout}")

    def test_list_shows_status_column(self):
        # The list output's header should label the status column.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["list"])
            self.assertIn("STATUS", r.stdout)

    def test_show_for_actor_with_no_session_doesnt_print_session_field(self):
        # Idle actor with no agent_session — show shouldn't print
        # "session: None" or "session id: ".
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            # Should not have "None" or empty-trailing session display.
            self.assertNotIn("session: None", r.stdout.lower())
            self.assertNotIn("session id: \n", r.stdout.lower())

    def test_logs_for_idle_actor_says_no_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["logs", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            err = (r.stdout + r.stderr).lower()
            self.assertTrue(
                "no run" in err or "no log" in err or "yet" in err
                or len(r.stdout.strip()) == 0,
                f"logs for idle actor should say 'no runs' or be empty: {r.stdout}",
            )

    def test_actor_new_message_includes_directory(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertIn(actor.dir, r.stdout)

    def test_run_failure_includes_exit_code_in_message(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "do x"],
                            **claude_responds("oops", exit=42))
            # Exit code 42 should be referenced in the output.
            self.assertIn("42", r.stdout + r.stderr)

    def test_logs_for_actor_with_just_one_run_no_extra_padding(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["logs", "alice"])
            # Output shouldn't have a huge amount of trailing whitespace.
            trailing_blanks = 0
            for line in reversed(r.stdout.splitlines()):
                if not line.strip():
                    trailing_blanks += 1
                else:
                    break
            self.assertLess(trailing_blanks, 5,
                            f"logs has {trailing_blanks} trailing blank lines")


if __name__ == "__main__":
    unittest.main()
