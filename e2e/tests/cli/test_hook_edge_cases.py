"""e2e: lifecycle hook edge cases."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class HookEdgeCaseTests(unittest.TestCase):

    def test_hook_command_not_found_aborts_create(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n    on-start "/path/that/does/not/exist arg"\n}\n'
            )
            r = env.run_cli(["new", "alice"])
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(env.list_actor_names(), [])

    def test_hook_writes_to_actor_dir_env_var(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-start "echo $ACTOR_DIR > $ACTOR_DIR/where.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            from pathlib import Path
            text = (Path(actor.dir) / "where.txt").read_text().strip()
            self.assertEqual(text, actor.dir)

    def test_hook_can_use_actor_session_id_var(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "echo $ACTOR_SESSION_ID > $ACTOR_DIR/sid.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            actor = env.fetch_actor("alice")
            from pathlib import Path
            self.assertTrue((Path(actor.dir) / "sid.txt").exists())

    def test_before_run_hook_failure_leaves_no_run_row(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.write_settings_kdl(
                'hooks {\n    before-run "exit 5"\n}\n'
            )
            env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            with env.db() as db:
                runs, total = db.list_runs("alice", limit=10)
            self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
