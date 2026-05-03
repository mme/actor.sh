"""e2e CLI: probes for the final couple of failing assertions."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds, codex_responds
from e2e.harness.isolated_home import isolated_home


class FinalTwoTests(unittest.TestCase):

    def test_logs_for_codex_actor_works(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x", "--agent", "codex"],
                        **codex_responds("CODEX_REPLY"))
            r = env.run_cli(["logs", "alice"])
            self.assertIn("CODEX_REPLY", r.stdout)

    def test_show_for_codex_actor_works(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--agent", "codex"])
            r = env.run_cli(["show", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("codex", r.stdout.lower())

    def test_actor_list_distinguishes_done_from_idle_visually(self):
        # Done actor should look different from idle actor in output.
        with isolated_home() as env:
            env.run_cli(["new", "done-actor", "do x"], **claude_responds("ok"))
            env.run_cli(["new", "idle-actor"])
            r = env.run_cli(["list"])
            done_line = [l for l in r.stdout.splitlines() if "done-actor" in l]
            idle_line = [l for l in r.stdout.splitlines() if "idle-actor" in l]
            self.assertEqual(len(done_line), 1)
            self.assertEqual(len(idle_line), 1)
            self.assertNotEqual(
                done_line[0].replace("done-actor", "").strip(),
                idle_line[0].replace("idle-actor", "").strip(),
                "done and idle rows should differ visually",
            )

    def test_actor_list_columns_are_consistent_widths(self):
        with isolated_home() as env:
            env.run_cli(["new", "short"])
            env.run_cli(["new", "much-longer-actor-name"])
            r = env.run_cli(["list"])
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            self.assertGreater(len(lines), 1)
            # The status word should start at the same column for both rows.
            status_pos = []
            for line in lines[1:]:
                idx = -1
                for word in ("idle", "done", "running", "error", "stopped"):
                    p = line.lower().find(word)
                    if p >= 0:
                        idx = p
                        break
                status_pos.append(idx)
            # All status positions should match (column-aligned table).
            self.assertEqual(len(set(p for p in status_pos if p > 0)), 1,
                             f"status column should be aligned: {status_pos}")


if __name__ == "__main__":
    unittest.main()
