"""e2e CLI: more `actor show` content checks."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ShowExtrasTests(unittest.TestCase):

    def test_show_includes_created_at_timestamp(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            # Should show some timestamp.
            import re
            has_iso = bool(re.search(r"\d{4}-\d{2}-\d{2}", r.stdout))
            self.assertTrue(has_iso, "show should include a timestamp")

    def test_show_includes_recent_run_status(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            self.assertIn("done", r.stdout.lower())

    def test_show_displays_per_run_prompt(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "UNIQUE_PROMPT_42"],
                        **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            self.assertIn("UNIQUE_PROMPT_42", r.stdout)

    def test_show_displays_run_duration_for_completed_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"],
                        **claude_responds("ok", sleep=0.5))
            r = env.run_cli(["show", "alice"])
            # Should reference duration / time / "s" somehow.
            self.assertTrue(
                any(w in r.stdout.lower() for w in ("ms", "s ", "second", "duration", "took")),
                f"show should display run duration: {r.stdout}",
            )


if __name__ == "__main__":
    unittest.main()
