"""e2e: ACTOR_RUN_ID and ACTOR_DURATION_MS env vars on after-run."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class HookAfterRunMetadataTests(unittest.TestCase):

    def test_actor_run_id_is_integer(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_RUN_ID > $HOME/runid.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            text = (env.home / "runid.txt").read_text().strip()
            self.assertTrue(text.isdigit())

    def test_consecutive_runs_have_different_run_ids(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_RUN_ID >> $HOME/runids.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            ids = (env.home / "runids.txt").read_text().strip().splitlines()
            self.assertEqual(len(set(ids)), 2,
                             f"expected distinct run IDs, got {ids}")


if __name__ == "__main__":
    unittest.main()
