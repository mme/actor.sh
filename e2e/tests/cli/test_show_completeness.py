"""e2e: `actor show` should expose all relevant actor metadata."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ShowCompletenessTests(unittest.TestCase):

    def test_show_includes_parent_field_when_set(self):
        # Manually insert a child actor with parent.
        with isolated_home() as env:
            env.run_cli(["new", "parent"])
            from actor.types import Actor, ActorConfig, AgentKind
            with env.db() as db:
                db.insert_actor(Actor(
                    name="child", agent=AgentKind.CLAUDE, agent_session=None,
                    dir=str(env.cwd), source_repo=None, base_branch=None,
                    worktree=False, parent="parent", config=ActorConfig(),
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                ))
            r = env.run_cli(["show", "child"])
            self.assertIn("parent", r.stdout)

    def test_show_includes_base_branch(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            # base_branch is set on worktree creation; should show it.
            # Worktree forked from "main".
            self.assertIn("main", r.stdout)

    def test_show_no_runs_section_for_idle(self):
        # An idle actor should not have a long runs section.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["show", "alice"])
            # Lines should be limited (no run history).
            line_count = len(r.stdout.splitlines())
            self.assertLess(line_count, 20,
                            f"idle actor's show output should be compact, got {line_count} lines")


if __name__ == "__main__":
    unittest.main()
