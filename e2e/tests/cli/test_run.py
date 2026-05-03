"""e2e: `actor run` — re-running existing actors with new prompts."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class ActorRunTests(unittest.TestCase):

    def test_run_existing_actor(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice", "do thing"], **claude_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 1)
            self.assertEqual(invs[0]["parsed"]["prompt"], "do thing")

    def test_run_resumes_session(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "first"], **claude_responds("ok1"))
            env.run_cli(["run", "alice", "second"], **claude_responds("ok2"))
            invs = env.claude_invocations()
            self.assertEqual(len(invs), 2)
            # The second invocation should pass --resume with the first's session id.
            second_parsed = invs[1]["parsed"]
            first_session = invs[0]["parsed"]["session_id"]
            self.assertIsNotNone(first_session)
            self.assertEqual(second_parsed["resume"], first_session)

    def test_run_reads_stdin(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            r = env.run_cli(["run", "alice"], input="from stdin",
                            **claude_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertEqual(invs[-1]["parsed"]["prompt"], "from stdin")

    def test_run_per_call_config_override(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus"])
            env.run_cli(["run", "alice", "do x", "--config", "model=haiku"],
                        **claude_responds("ok"))
            invs = env.claude_invocations()
            self.assertEqual(invs[-1]["parsed"]["model"], "haiku")
            # The persisted config stays at opus.
            actor = env.fetch_actor("alice")
            self.assertEqual(actor.config.agent_args.get("model"), "opus")

    def test_run_missing_actor_errors(self):
        with isolated_home() as env:
            r = env.run_cli(["run", "ghost", "do x"])
            self.assertNotEqual(r.returncode, 0)

    def test_run_fires_before_run_hook(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    before-run "echo BEFORE >> $ACTOR_DIR/hook.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            actor = env.fetch_actor("alice")
            from pathlib import Path
            self.assertIn("BEFORE", (Path(actor.dir) / "hook.txt").read_text())

    def test_run_before_run_hook_failure_aborts(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.write_settings_kdl(
                'hooks {\n    before-run "exit 1"\n}\n'
            )
            r = env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(env.claude_invocations(), [])

    def test_run_fires_after_run_hook_with_metadata(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    after-run "env | grep ^ACTOR_ >> $ACTOR_DIR/after.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
            actor = env.fetch_actor("alice")
            from pathlib import Path
            text = (Path(actor.dir) / "after.txt").read_text()
            self.assertIn("ACTOR_RUN_ID", text)
            self.assertIn("ACTOR_EXIT_CODE", text)
            self.assertIn("ACTOR_DURATION_MS", text)


if __name__ == "__main__":
    unittest.main()
