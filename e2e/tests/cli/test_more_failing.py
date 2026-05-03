"""e2e CLI: more probes targeting potential failures."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class MoreFailingTests(unittest.TestCase):

    def test_show_displays_role_derived_config_keys(self):
        # The role's config snapshots into the actor at create time;
        # show displays the merged config. Role identity itself isn't
        # tracked on the actor, but the resulting config is visible.
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    description "Q"\n'
                '    agent "claude"\n'
                '    model "opus"\n'
                '}\n'
            )
            env.run_cli(["new", "alice", "--role", "qa"])
            r = env.run_cli(["show", "alice"])
            self.assertIn("opus", r.stdout)

    def test_list_distinguishes_actors_with_different_agents(self):
        from e2e.harness.fakes_control import codex_responds
        with isolated_home() as env:
            env.run_cli(["new", "claude-actor"])
            env.run_cli(["new", "codex-actor", "--agent", "codex"])
            r = env.run_cli(["list"])
            # Should distinguish the two — at minimum, both names show.
            self.assertIn("claude-actor", r.stdout)
            self.assertIn("codex-actor", r.stdout)
            # Stronger: list output should let user tell them apart.
            self.assertTrue(
                "claude" in r.stdout.lower() and "codex" in r.stdout.lower(),
                f"list should distinguish agents: {r.stdout}",
            )

    def test_actor_show_for_actor_with_run_includes_finished_at(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["show", "alice"])
            # Should mention when the run finished.
            import re
            timestamps = re.findall(r"\d{2}:\d{2}", r.stdout)
            self.assertGreater(len(timestamps), 0,
                               f"show should include time: {r.stdout}")

    def test_run_with_failing_run_shows_error_in_output(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "do x"],
                            **claude_responds("oops", exit=2))
            # Should indicate the run failed in some way.
            self.assertTrue(
                "error" in (r.stdout + r.stderr).lower()
                or "fail" in (r.stdout + r.stderr).lower(),
                f"failed run should indicate error: out={r.stdout!r} err={r.stderr!r}",
            )

    def test_actor_logs_doesnt_show_internal_implementation_details(self):
        # Things like "session_id", "thread_id" raw fields shouldn't
        # appear in non-verbose logs.
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            r = env.run_cli(["logs", "alice"])
            self.assertNotIn("session_id", r.stdout)


if __name__ == "__main__":
    unittest.main()
