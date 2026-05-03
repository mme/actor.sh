"""e2e TUI: status polling reflects external state changes."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import wait_for_actor_in_tree, watch_app


class StatusPollingTests(unittest.IsolatedAsyncioTestCase):

    async def test_externally_created_actor_appears_in_tree(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                # Boot first, then create the actor via CLI in another
                # process. The watch app's poll cycle should pick it up.
                env.run_cli(["new", "alice"])
                await wait_for_actor_in_tree(pilot, app, "alice", timeout=8)


if __name__ == "__main__":
    unittest.main()
