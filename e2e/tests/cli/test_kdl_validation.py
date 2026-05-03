"""e2e: settings.kdl validation rules surface as user-facing errors."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class KdlValidationTests(unittest.TestCase):

    def test_duplicate_role_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n    agent "claude"\n}\n'
                'role "qa" {\n    agent "codex"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("duplicate", r.stderr.lower() + r.stdout.lower())

    def test_duplicate_defaults_block_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "claude" {\n    model "opus"\n}\n'
                'defaults "claude" {\n    model "sonnet"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)

    def test_duplicate_hooks_block_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n    on-start "echo a"\n}\n'
                'hooks {\n    on-start "echo b"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)

    def test_duplicate_ask_block_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'ask {\n    on-start "a"\n}\n'
                'ask {\n    on-start "b"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)

    def test_unknown_agent_in_defaults_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'defaults "bogus-agent" {\n    model "opus"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)

    def test_unknown_hook_name_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'hooks {\n    on-bogus "echo"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)

    def test_unknown_ask_key_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'ask {\n    after-run "nope"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)

    def test_role_without_name_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role {\n    agent "claude"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
