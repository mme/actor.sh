"""e2e workflow: hooks fire at the right times with the right env."""
from __future__ import annotations

import unittest
from pathlib import Path

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class HookLifecycleTests(unittest.TestCase):

    def test_full_lifecycle_hooks_fire_in_order(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-start "echo on-start >> $HOME/order.txt"\n'
                '    before-run "echo before-run >> $HOME/order.txt"\n'
                '    after-run "echo after-run >> $HOME/order.txt"\n'
                '    on-discard "echo on-discard >> $HOME/order.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            env.run_cli(["discard", "alice"])
            order = (env.home / "order.txt").read_text().strip().splitlines()
            self.assertEqual(order, [
                "on-start", "before-run", "after-run", "on-discard"
            ])

    def test_hook_env_includes_actor_metadata(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-start "env | grep ^ACTOR_ > $HOME/env.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            text = (env.home / "env.txt").read_text()
            self.assertIn("ACTOR_NAME=alice", text)
            self.assertIn("ACTOR_AGENT=claude", text)
            self.assertIn("ACTOR_DIR=", text)

    def test_project_hook_overrides_user_hook(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n    on-start "echo USER >> $HOME/marker.txt"\n}\n',
                scope="user",
            )
            env.write_settings_kdl(
                'hooks {\n    on-start "echo PROJECT >> $HOME/marker.txt"\n}\n',
                scope="project",
            )
            env.run_cli(["new", "alice"])
            text = (env.home / "marker.txt").read_text()
            self.assertIn("PROJECT", text)
            self.assertNotIn("USER", text)


if __name__ == "__main__":
    unittest.main()
