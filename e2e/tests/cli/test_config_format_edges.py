"""e2e CLI: config pair format edge cases."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class ConfigFormatEdgeTests(unittest.TestCase):

    def test_config_pair_with_no_value_after_equals(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--config", "model="])
            self.assertNotIn("Traceback", r.stderr)

    def test_config_pair_with_no_equals_at_all(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--config", "modelopus"])
            # Either error or stored as key with empty value.
            self.assertNotIn("Traceback", r.stderr)

    def test_config_pair_with_only_equals(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--config", "="])
            self.assertNotIn("Traceback", r.stderr)

    def test_config_with_spaces_in_value(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--config", "system-prompt=hello world"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertEqual(
                actor.config.agent_args.get("system-prompt"), "hello world"
            )


if __name__ == "__main__":
    unittest.main()
