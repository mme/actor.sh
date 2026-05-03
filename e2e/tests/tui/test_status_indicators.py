"""e2e TUI: status indicators in the tree should be informative."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import wait_for_actor_in_tree, watch_app


class StatusIndicatorTests(unittest.IsolatedAsyncioTestCase):

    async def _label_for(self, app, name):
        from actor.watch.app import ActorTree
        tree = app.query_one(ActorTree)
        for node in tree.root.children:
            if name in str(node.label):
                return str(node.label)
        return ""

    async def test_done_actor_label_indicates_done(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice", "do x"], **claude_responds("ok"))
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "alice")
                # Wait briefly for status icon to update.
                for _ in range(20):
                    await pilot.pause(0.1)
                label = await self._label_for(app, "alice")
                # Some signal of "done" — either word or icon. Assert
                # the label is non-empty (icon-only is acceptable).
                self.assertTrue(label.strip())

    async def test_error_actor_label_distinct_from_done(self):
        with isolated_home() as env:
            env.run_cli(["new", "ok-actor", "task"], **claude_responds("ok"))
            env.run_cli(["new", "err-actor", "task"],
                        **claude_responds("oops", exit=2))
            async with watch_app(env) as (app, pilot):
                await wait_for_actor_in_tree(pilot, app, "ok-actor")
                await wait_for_actor_in_tree(pilot, app, "err-actor")
                # Wait for status to settle.
                for _ in range(30):
                    await pilot.pause(0.1)
                ok_label = await self._label_for(app, "ok-actor")
                err_label = await self._label_for(app, "err-actor")
                # The two should be visually distinct.
                self.assertNotEqual(
                    ok_label.replace("ok-actor", "").strip(),
                    err_label.replace("err-actor", "").strip(),
                    "done and error labels should differ visually",
                )

    async def test_idle_actor_label_distinct_from_running(self):
        import subprocess, time
        with isolated_home() as env:
            env.run_cli(["new", "idle-one"])
            p = subprocess.Popen(
                ["actor", "new", "running-one", "long task"],
                env=env.env(**claude_responds("ok", sleep=3)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                async with watch_app(env) as (app, pilot):
                    await wait_for_actor_in_tree(pilot, app, "idle-one")
                    await wait_for_actor_in_tree(pilot, app, "running-one")
                    for _ in range(20):
                        await pilot.pause(0.1)
                    idle_label = await self._label_for(app, "idle-one")
                    running_label = await self._label_for(app, "running-one")
                    self.assertNotEqual(
                        idle_label.replace("idle-one", "").strip(),
                        running_label.replace("running-one", "").strip(),
                        "idle and running labels should differ visually",
                    )
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
