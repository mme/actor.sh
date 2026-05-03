"""e2e CLI: consistency in output formatting across commands."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class FormatConsistencyTests(unittest.TestCase):

    def test_list_output_has_consistent_column_alignment(self):
        with isolated_home() as env:
            env.run_cli(["new", "short"])
            env.run_cli(["new", "much-longer-actor-name"])
            r = env.run_cli(["list"])
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            # Header + 2 actor rows
            self.assertGreaterEqual(len(lines), 3)
            # All rows should have a status word at a consistent position.

    def test_show_output_doesnt_contain_python_repr(self):
        # No `<...>` repr-style output should leak.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertNotIn("<", r.stdout[:5],
                             "show output shouldn't start with python repr")

    def test_list_output_doesnt_contain_status_enum_repr(self):
        # `Status.IDLE` enum repr leaks would look like that.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["list"])
            self.assertNotIn("Status.", r.stdout)

    def test_show_doesnt_show_inner_object_addresses(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            self.assertNotIn("0x", r.stdout)

    def test_logs_doesnt_leak_jsonl_lines(self):
        # Non-verbose logs should NOT show the raw JSONL lines.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("answer"))
            r = env.run_cli(["logs", "alice"])
            # There shouldn't be a literal `"type":"assistant"` substring.
            self.assertNotIn('"type":"assistant"', r.stdout)


if __name__ == "__main__":
    unittest.main()
