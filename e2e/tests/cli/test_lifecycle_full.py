"""e2e: full actor lifecycle — new → run → run → stop → run → discard."""
from __future__ import annotations

import time
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class FullLifecycleTests(unittest.TestCase):

    def test_create_run_run_discard(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            env.run_cli(["run", "alice", "third"], **claude_responds("c"))
            with env.db() as db:
                _, total = db.list_runs("alice", limit=10)
                self.assertEqual(total, 3)
            env.run_cli(["discard", "alice", "--force"])
            self.assertEqual(env.list_actor_names(), [])

    def test_session_id_preserved_across_runs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("a"))
            actor1 = env.fetch_actor("alice")
            env.run_cli(["run", "alice", "second"], **claude_responds("b"))
            actor2 = env.fetch_actor("alice")
            self.assertEqual(actor1.agent_session, actor2.agent_session)

    def test_run_count_grows_per_call(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            for i in range(5):
                env.run_cli(["run", "alice", f"run {i}"], **claude_responds("ok"))
            with env.db() as db:
                _, total = db.list_runs("alice", limit=20)
                self.assertEqual(total, 5)


if __name__ == "__main__":
    unittest.main()
