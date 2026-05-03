"""e2e: `actor config` — view + update saved actor config."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorConfigTests(unittest.TestCase):

    def test_config_view(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            r = env.run_cli(["config", "alice"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("model", r.stdout)
            self.assertIn("opus", r.stdout)

    def test_config_update_single_pair(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["config", "alice", "model=sonnet"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "sonnet")

    def test_config_update_multiple_pairs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["config", "alice", "model=opus", "effort=max"])
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "opus")
            self.assertEqual(actor.config.agent_args.get("effort"), "max")

    def test_config_clear_value_with_empty_pair(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            # `key=` with no value should clear the key.
            env.run_cli(["config", "alice", "model="])
            actor = env.fetch_actor("alice")
            self.assertNotIn("model", actor.config.agent_args)

    def test_config_change_does_not_affect_in_flight_run(self):
        # While a run is mid-flight, config changes are deferred to the
        # next run — not the in-flight one.
        import subprocess, time
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            p = subprocess.Popen(
                ["actor", "run", "alice", "do x"],
                env=env.env(**claude_responds("ok", sleep=2)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.3)
                env.run_cli(["config", "alice", "model=haiku"])
                p.wait(timeout=10)
            finally:
                if p.poll() is None:
                    p.kill()
            invs = env.claude_invocations()
            # The in-flight run was launched with `opus`.
            self.assertEqual(invs[0]["parsed"]["model"], "opus")

    def test_config_change_takes_effect_on_next_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["config", "alice", "model=opus"])
            env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(invs[-1]["parsed"]["model"], "opus")


if __name__ == "__main__":
    unittest.main()
