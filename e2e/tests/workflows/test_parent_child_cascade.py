"""e2e workflow: parent-child relationships + discard cascade."""
from __future__ import annotations

import os
import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ParentChildCascadeTests(unittest.TestCase):

    def test_actor_spawned_with_actor_name_env_records_parent(self):
        # When the fake claude spawns a child that calls `actor new`,
        # the child should record the parent via $ACTOR_NAME.
        with isolated_home() as env:
            spawn_cmd = (
                f"ACTOR_NAME=parent {env.run_cli.__self__.env.__func__.__qualname__}"
            ) if False else None
            # Easier: spawn the child directly via FAKE_CLAUDE_SPAWN_CHILD
            # which inherits env (including ACTOR_NAME set by the parent's run).
            env.run_cli(["new", "parent", "do x"], **claude_responds(
                "spawned",
                spawn_child=f"actor new child --no-worktree",
            ))
            # Wait briefly for the detached child to insert its row.
            import time
            for _ in range(20):
                time.sleep(0.1)
                if "child" in env.list_actor_names():
                    break
            actor = env.fetch_actor("child")
            self.assertEqual(actor.parent, "parent")

    def test_discard_parent_cascades_to_children(self):
        with isolated_home() as env:
            env.run_cli(["new", "parent"])
            # Simulate child by manually inserting an actor with parent=parent.
            from actor.types import Actor, ActorConfig, AgentKind
            with env.db() as db:
                db.insert_actor(Actor(
                    name="child", agent=AgentKind.CLAUDE, agent_session=None,
                    dir=str(env.cwd), source_repo=None, base_branch=None,
                    worktree=False, parent="parent", config=ActorConfig(),
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                ))
            r = env.run_cli(["discard", "parent"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(env.list_actor_names(), [])


if __name__ == "__main__":
    unittest.main()
