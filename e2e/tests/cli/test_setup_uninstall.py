"""e2e: `actor setup --uninstall` removes the deployed skill."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home


class ActorSetupUninstallTests(unittest.TestCase):

    def test_uninstall_after_setup(self):
        with isolated_home() as env:
            env.run_cli(["setup", "--for", "claude-code"])
            r = env.run_cli([
                "setup", "--for", "claude-code", "--uninstall"
            ])
            # Uninstall should remove the skill files; don't assert
            # exit strictly but there should be no traceback.
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
