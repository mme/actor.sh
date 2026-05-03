"""e2e TUI: poll loop resilience to external state changes."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, watch_app


class PollingResilienceTests(unittest.IsolatedAsyncioTestCase):

    async def test_poll_handles_actor_added_while_focused_elsewhere(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Add another actor externally.
                env.run_cli(["new", "bob"])
                # Wait for poll cycle.
                from actor.watch.app import ActorTree
                for _ in range(40):
                    await pilot.pause(0.1)
                    tree = app.query_one(ActorTree)
                    names = [str(n.label) for n in tree.root.children]
                    if any("bob" in n for n in names):
                        break
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_poll_handles_currently_selected_actor_being_modified(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # External run.
                env.run_cli(["run", "alice", "do x"], **claude_responds("ok"))
                for _ in range(30):
                    await pilot.pause(0.1)
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_poll_handles_db_being_locked_briefly(self):
        # Open another DB connection to hold a lock briefly.
        import threading, time
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                # Hold a write lock for 500ms.
                def _hold():
                    import sqlite3
                    conn = sqlite3.connect(env.home / ".actor" / "actor.db",
                                          isolation_level="EXCLUSIVE")
                    cur = conn.cursor()
                    cur.execute("BEGIN EXCLUSIVE")
                    time.sleep(0.5)
                    conn.rollback()
                    conn.close()
                t = threading.Thread(target=_hold, daemon=True)
                t.start()
                # Let polling cycle hit the lock.
                for _ in range(20):
                    await pilot.pause(0.1)
                t.join(timeout=2)
                # Watch should not have died.
                self.assertIsNone(getattr(app, "_exception", None))


if __name__ == "__main__":
    unittest.main()
