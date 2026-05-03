"""e2e: roles can carry actor-key whitelist entries (use-subscription)."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class RoleUseSubscriptionTests(unittest.TestCase):

    def test_role_carries_use_subscription_actor_key(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    use-subscription false\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.actor_keys.get("use-subscription"), "false"
            )

    def test_role_with_unknown_key_routes_to_agent_args(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    weird-future-flag "xyz"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            actor = env.fetch_actor("alice")
            # Unknown keys go into agent_args (forwarded as CLI flags).
            self.assertEqual(
                actor.config.agent_args.get("weird-future-flag"), "xyz"
            )


if __name__ == "__main__":
    unittest.main()
