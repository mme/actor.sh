"""e2e: per-agent `defaults "<n>" { ... }` blocks at the CLI."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class DefaultsBlockTests(unittest.TestCase):

    def test_claude_defaults_apply_to_new_actor(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n'
                '    model "opus"\n'
                '    permission-mode "bypassPermissions"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "opus")
            self.assertEqual(
                actor.config.agent_args.get("permission-mode"),
                "bypassPermissions",
            )

    def test_codex_defaults_apply_to_new_actor(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "codex" {\n'
                '    m "o3"\n'
                '    sandbox "read-only"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--agent", "codex"])
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("m"), "o3")
            self.assertEqual(actor.config.agent_args.get("sandbox"), "read-only")

    def test_use_subscription_defaults_to_actor_keys(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n'
                '    use-subscription false\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.actor_keys.get("use-subscription"), "false"
            )

    def test_null_in_defaults_cancels_class_default(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n'
                '    permission-mode null\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertNotIn("permission-mode", actor.config.agent_args)

    def test_project_defaults_override_user(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n    model "user-model"\n}\n',
                scope="user",
            )
            env.write_settings_kdl(
                'defaults "claude" {\n    model "project-model"\n}\n',
                scope="project",
            )
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.agent_args.get("model"), "project-model"
            )

    def test_cli_config_beats_defaults_block(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n    model "opus"\n}\n'
            )
            env.run_cli(["new", "alice", "--config", "model=haiku"])
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "haiku")

    def test_defaults_apply_at_run_time_via_snapshot(self):
        # Defaults snapshot at create time. Editing settings.kdl
        # later doesn't retroactively change the actor's config.
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n    model "opus"\n}\n'
            )
            env.run_cli(["new", "alice"])
            # Mutate settings.kdl AFTER creation.
            env.write_settings_kdl(
                'defaults "claude" {\n    model "haiku"\n}\n'
            )
            actor = env.fetch_actor("alice")
            # Snapshot is opus, not haiku.
            self.assertEqual(actor.config.agent_args.get("model"), "opus")


if __name__ == "__main__":
    unittest.main()
