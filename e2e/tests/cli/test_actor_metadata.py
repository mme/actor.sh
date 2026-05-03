"""e2e: actor metadata fields are correctly populated."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorMetadataTests(unittest.TestCase):

    def test_created_at_set(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertIsNotNone(actor.created_at)
            self.assertNotEqual(actor.created_at, "")

    def test_updated_at_advances_on_run(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor1 = env.fetch_actor("alice")
            import time
            time.sleep(1.1)
            env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            actor2 = env.fetch_actor("alice")
            self.assertGreater(actor2.updated_at, actor1.updated_at)

    def test_agent_field_persists(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--agent", "codex"])
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.agent.value, "codex")

    def test_source_repo_set_for_worktree_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            self.assertIsNotNone(actor.source_repo)
            self.assertIn(str(env.cwd), actor.source_repo)

    def test_source_repo_none_for_no_worktree_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--no-worktree"])
            actor = env.fetch_actor("alice")
            self.assertIsNone(actor.source_repo)


if __name__ == "__main__":
    unittest.main()
