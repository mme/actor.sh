"""e2e: settings.kdl error messages should be user-friendly."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class KdlErrorMessageTests(unittest.TestCase):

    def _err(self, kdl: str) -> str:
        with isolated_home() as env:
            env.write_settings_kdl(kdl)
            r = env.run_cli(["roles"])
            return r.stderr + r.stdout

    def test_malformed_kdl_mentions_path(self):
        msg = self._err("role \"qa\" { unclosed string\n")
        self.assertIn("settings.kdl", msg.lower())

    def test_unknown_role_field_kdl_doesnt_crash(self):
        # Unknown field inside a role should either parse-into-config or
        # be flagged. Either way, no traceback.
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n    agent "claude"\n    weird-key "value"\n}\n'
            )
            r = env.run_cli(["roles"])
            self.assertNotIn("Traceback", r.stderr)

    def test_role_with_empty_name_rejected(self):
        with isolated_home() as env:
            env.write_settings_kdl('role "" {\n    agent "claude"\n}\n')
            r = env.run_cli(["roles"])
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
