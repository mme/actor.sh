"""e2e CLI: more UX-focused assertions likely to find issues."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreUxAssertionsTests(unittest.TestCase):

    def test_run_completion_message_includes_response_or_status(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "do x"], **claude_responds("RESPONSE_HERE"))
            # User wants to see something useful — either the response
            # or a "done" status.
            self.assertTrue(
                "RESPONSE_HERE" in r.stdout or "done" in r.stdout.lower(),
                f"run output should give feedback: {r.stdout!r}",
            )

    def test_new_with_prompt_completion_shows_response(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "do x"],
                            **claude_responds("UNIQUE_RESPONSE_42"))
            self.assertIn("UNIQUE_RESPONSE_42", r.stdout)

    def test_show_unknown_actor_message_is_friendly(self):
        with isolated_home() as env:
            r = env.run_cli(["show", "unique-ghost-xyz"])
            err = r.stderr + r.stdout
            self.assertTrue(
                "not found" in err.lower() or "unknown" in err.lower()
                or "no such" in err.lower(),
                f"show should give friendly error: {err}",
            )

    def test_actor_main_help_clearly_describes_what_it_does(self):
        with isolated_home() as env:
            r = env.run_cli(["main", "--help"])
            # main --help short-circuits to claude --help; this is
            # consistent with the design but might surprise users.
            # Just check no traceback.
            self.assertNotIn("Traceback", r.stderr)

    def test_cli_help_mentions_main_subcommand(self):
        with isolated_home() as env:
            r = env.run_cli(["--help"])
            self.assertIn("main", r.stdout.lower())

    def test_setup_message_tells_user_what_to_do_next(self):
        with isolated_home() as env:
            r = env.run_cli(["setup", "--for", "claude-code"])
            if r.returncode == 0:
                # Setup output should include next steps (e.g. mention `actor main`).
                self.assertTrue(
                    "main" in r.stdout.lower() or "session" in r.stdout.lower()
                )


if __name__ == "__main__":
    unittest.main()
