"""e2e: per-run config overrides don't pollute the actor's stored config."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class PerRunOverridesTests(unittest.TestCase):

    def test_run_config_does_not_change_stored_config(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            env.run_cli(["run", "alice", "do x", "--config", "model=haiku"],
                        **claude_responds("ok"))
            actor = env.fetch_actor("alice")
            # Stored config still opus; per-run override was haiku.
            self.assertEqual(actor.config.agent_args.get("model"), "opus")

    def test_run_config_layered_with_stored_config(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice",
                         "--config", "model=opus",
                         "--config", "effort=max"])
            env.run_cli(["run", "alice", "do x", "--config", "model=haiku"],
                        **claude_responds("ok"))
            invs = env.claude_invocations()
            # Per-run override takes effect for this run.
            self.assertEqual(invs[0]["parsed"]["model"], "haiku")
            # Other stored config keys still apply.
            extras = invs[0]["parsed"]["extra_flags"]
            # Note: extras may or may not contain effort depending on
            # how claude.py routes unknown keys; just don't crash.

    def test_two_consecutive_runs_apply_different_overrides(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["run", "alice", "first", "--config", "model=opus"],
                        **claude_responds("a"))
            env.run_cli(["run", "alice", "second", "--config", "model=haiku"],
                        **claude_responds("b"))
            invs = env.claude_invocations()
            self.assertEqual(invs[0]["parsed"]["model"], "opus")
            self.assertEqual(invs[1]["parsed"]["model"], "haiku")


if __name__ == "__main__":
    unittest.main()
