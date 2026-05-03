"""e2e: pathological / adversarial inputs to surface latent bugs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home


class PathologicalInputTests(unittest.TestCase):

    def test_config_with_equals_in_value(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "--config", "model=opus=v2"])
            actor = env.fetch_actor("alice")
            # Value should keep the second `=` (only the first splits).
            self.assertEqual(actor.config.agent_args.get("model"), "opus=v2")

    def test_config_with_no_equals_rejected(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "alice", "--config", "modelopus"])
            # Either rejected or stored as key with empty value — should
            # not crash.
            self.assertNotIn("Traceback", r.stderr)

    def test_role_name_with_special_chars(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa-engineer" {\n    agent "claude"\n}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa-engineer"])
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_role_with_quotes_in_prompt(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    agent "claude"\n'
                '    prompt "you said \\"hello\\""\n'
                '}\n'
            )
            r = env.run_cli(["new", "alice", "--role", "qa", "do x"],
                            **claude_responds("ok"))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            invs = env.claude_invocations()
            self.assertIn('"hello"', invs[0]["parsed"]["append_system_prompt"])

    def test_hook_command_with_pipes(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n'
                '    on-start "echo hi | tr a-z A-Z > $ACTOR_DIR/upper.txt"\n'
                '}\n'
            )
            env.run_cli(["new", "alice"])
            actor = env.fetch_actor("alice")
            from pathlib import Path
            self.assertEqual(
                (Path(actor.dir) / "upper.txt").read_text().strip(), "HI"
            )

    def test_actor_with_only_digits_name(self):
        with isolated_home() as env:
            r = env.run_cli(["new", "12345"])
            # validate_name allows starting with a number.
            self.assertEqual(r.returncode, 0, msg=r.stderr)


if __name__ == "__main__":
    unittest.main()
