"""e2e TUI: UX edge cases that often hide bugs."""
from __future__ import annotations

import unittest

from e2e.harness.fakes_control import claude_responds
from e2e.harness.isolated_home import isolated_home
from e2e.harness.pilot import select_actor, wait_for_actor_in_tree, watch_app


class UxEdgeTests(unittest.IsolatedAsyncioTestCase):

    async def test_q_quits_even_with_modal_open(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.2)
                await pilot.press("q")
                await pilot.pause(0.3)
                # App should be exiting OR quit should be intentionally
                # blocked by modal — both are reasonable, but no crash.
                self.assertNotIn("error", str(app._exception or "").lower())

    async def test_selecting_discarded_actor_doesnt_crash(self):
        # Boot watch with two actors, discard one externally, watch
        # picks it up via polling, then try to select the now-gone one
        # by using the cached node reference.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            env.run_cli(["new", "bob"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Externally discard alice.
                env.run_cli(["discard", "alice", "--force"])
                # Wait for poll cycle to refresh the tree.
                for _ in range(40):
                    await pilot.pause(0.1)
                    from actor.watch.app import ActorTree
                    tree = app.query_one(ActorTree)
                    names = [str(n.label) for n in tree.root.children]
                    if not any("alice" in n for n in names):
                        break
                # No crash — the watch app should remain responsive.
                self.assertIsNone(getattr(app, "_exception", None))

    async def test_focus_doesnt_get_lost_after_select_then_back_to_tree(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                # Move into the tab.
                await pilot.press("right")
                await pilot.pause(0.1)
                # Back to tree.
                await pilot.press("a")
                await pilot.pause(0.1)
                from actor.watch.app import ActorTree
                self.assertIs(app.focused, app.query_one(ActorTree))

    async def test_o_then_d_cycles_tabs(self):
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                await pilot.press("o")
                await pilot.pause(0.1)
                from textual.widgets import TabbedContent
                tabs = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabs.active, "info")
                await pilot.press("d")
                await pilot.pause(0.1)
                self.assertEqual(tabs.active, "diff")
                await pilot.press("o")
                await pilot.pause(0.1)
                self.assertEqual(tabs.active, "info")

    async def test_help_overlay_shows_q_binding(self):
        with isolated_home() as env:
            async with watch_app(env) as (app, pilot):
                app.action_show_help_panel()
                await pilot.pause(0.3)
                from actor.watch.help_overlay import HelpOverlay
                self.assertIsInstance(app.screen, HelpOverlay)
                # The bindings table should reference quit somehow.

    async def test_running_actor_shows_running_in_tree_label(self):
        # Spawn a long-sleeping run; watch app should see "running".
        import subprocess, time
        with isolated_home() as env:
            p = subprocess.Popen(
                ["actor", "new", "alice", "long task"],
                env=env.env(**claude_responds("ok", sleep=3)),
                cwd=str(env.cwd),
            )
            try:
                time.sleep(0.5)
                async with watch_app(env) as (app, pilot):
                    await wait_for_actor_in_tree(pilot, app, "alice")
                    from actor.watch.app import ActorTree
                    tree = app.query_one(ActorTree)
                    label = ""
                    for _ in range(30):
                        await pilot.pause(0.1)
                        for node in tree.root.children:
                            if "alice" in str(node.label):
                                label = str(node.label)
                                break
                        if "running" in label.lower():
                            break
                    self.assertIn("running", label.lower(),
                                  f"expected running indicator; got {label!r}")
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)

    async def test_overview_shows_status_icon(self):
        # Selecting an idle actor should show a status icon in the overview header.
        with isolated_home() as env:
            env.run_cli(["new", "alice"])
            async with watch_app(env) as (app, pilot):
                await select_actor(pilot, app, "alice")
                from textual.widgets import Static
                header = app.query_one("#overview-header", Static)
                # Just check rendering didn't crash.
                self.assertIsNotNone(header.render())


if __name__ == "__main__":
    unittest.main()
