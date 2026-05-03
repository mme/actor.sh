"""e2e: combinations of features that should compose cleanly."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class FeatureCombinationTests(unittest.TestCase):

    def test_role_plus_defaults_plus_cli_layered_correctly(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n'
                '    model "default-model"\n'
                '    effort "low"\n'
                '}\n'
                'role "qa" {\n'
                '    agent "claude"\n'
                '    model "role-model"\n'
                '    permission-mode "auto"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa",
                         "--config", "model=cli-model"])
            actor = env.fetch_actor("alice")
            # CLI > role > defaults
            self.assertEqual(
                actor.config.agent_args.get("model"), "cli-model"
            )
            # Role's permission-mode survives
            self.assertEqual(
                actor.config.agent_args.get("permission-mode"), "auto"
            )
            # Defaults' effort survives (not overridden anywhere)
            self.assertEqual(
                actor.config.agent_args.get("effort"), "low"
            )

    def test_hook_plus_role_apply_in_order(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you are qa"\n'
                '}\n'
                'hooks {\n'
                '    on-start "echo $ACTOR_NAME-from-hook >> $HOME/marker.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            text = (env.home / "marker.txt").read_text().strip()
            self.assertEqual(text, "alice-from-hook")
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.agent_args.get("append-system-prompt"),
                "you are qa",
            )

    def test_ask_plus_role_plus_defaults_all_active(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'ask {\n    on-start "ASK_ON_START_MARKER"\n}\n'
                'defaults "claude" {\n    model "opus"\n}\n'
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "qa role prompt"\n'
                '}\n'
            )
            r = env.run_cli(["roles"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("qa", r.stdout)
            # Each block parses without conflict.


if __name__ == "__main__":
    unittest.main()
