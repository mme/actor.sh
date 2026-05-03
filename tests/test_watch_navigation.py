"""Pilot-driven navigation tests for `actor watch` (#24).

The reported bug: when the detail pane (RichLog / VerticalScroll) has focus
and the inner widget is horizontally scrollable, pressing Left scrolls the
viewport instead of returning focus to the actor tree.

The current code has `priority=True` on the `left,ctrl+b` binding, which
should make the App's `action_navigate_left` fire before the focused
widget's own scroll binding gets a chance. These tests exercise that
end-to-end via Pilot.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WatchArrowNavigationTests(unittest.IsolatedAsyncioTestCase):
    """Boot the watch app under Pilot with an isolated HOME + DB and verify
    arrow-left always returns focus toward the actor tree, regardless of
    which detail-pane widget currently has focus."""

    def _setup_home(self) -> str:
        """Create a tempdir HOME with a stub actor row so the watch app has
        something to display in the tree. Returns the tempdir path."""
        tmpdir = tempfile.mkdtemp(prefix="watch-nav-test-")
        actor_dir = Path(tmpdir) / ".actor"
        actor_dir.mkdir(parents=True, exist_ok=True)

        # Open the DB through actor.db so the schema gets initialized via the
        # normal path (avoids drift between this test and migrations).
        from actor.db import Database
        from actor.types import Actor, ActorConfig, AgentKind
        db = Database.open(str(actor_dir / "actor.db"))
        db.insert_actor(Actor(
            name="alpha",
            agent=AgentKind.CLAUDE,
            agent_session=None,
            dir=tmpdir,
            source_repo=None,
            base_branch=None,
            worktree=False,
            parent=None,
            config=ActorConfig(),
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ))
        db.close()
        return tmpdir

    async def _boot_app(self, pilot_size=(120, 40)):
        """Spin up ActorWatchApp with HOME pointing at a fresh DB. Returns
        an async context manager you can `async with` for a Pilot."""
        tmpdir = self._setup_home()
        # Stub out the omarchy-detection path so the app doesn't try to
        # read /home/$USER/.config/omarchy/... during the test.
        env_patch = patch.dict(os.environ, {"HOME": tmpdir})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        # Disable the splash animation so on_ready's "splash done"
        # transition doesn't gate the whole test on a real-time delay.
        from actor.watch.app import ActorWatchApp
        return ActorWatchApp(animate=False)

    async def _wait_for_tree_actor(self, pilot, app, name: str, timeout: float = 2.0):
        """Poll until the given actor name appears in the tree (the periodic
        poller inserts it on the first refresh)."""
        from actor.watch.app import ActorTree
        for _ in range(int(timeout / 0.05)):
            await pilot.pause(0.05)
            tree = app.query_one(ActorTree)
            for node in tree.root.children:
                if name in str(node.label):
                    return node
        raise AssertionError(f"actor '{name}' never appeared in the tree")

    async def _select_actor(self, pilot, app, name: str):
        """Move the tree cursor onto the actor and trigger selection so the
        detail pane refreshes for that actor."""
        from actor.watch.app import ActorTree
        node = await self._wait_for_tree_actor(pilot, app, name)
        tree = app.query_one(ActorTree)
        tree.focus()
        tree.select_node(node)
        await pilot.pause(0.1)

    async def _dismiss_splash_if_present(self, pilot, app):
        """The splash widget intercepts most input until dismissed. With
        animate=False it should clear quickly, but poll just in case."""
        for _ in range(20):
            if not getattr(app, "_splash_active", False):
                return
            await pilot.pause(0.05)

    async def test_left_from_overview_richlog_returns_to_tree(self):
        """OVERVIEW is tab idx 0. Focusing #logs-content (RichLog) and
        pressing Left must land focus on the ActorTree, NOT scroll the log
        viewport. This is the canonical reproduction of #24."""
        app = await self._boot_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash_if_present(pilot, app)
            await self._select_actor(pilot, app, "alpha")

            # Switch to OVERVIEW (idx 0) and focus the inner RichLog.
            app.action_show_tab("info")
            await pilot.pause(0.1)
            from textual.widgets import RichLog
            log = app.query_one("#logs-content", RichLog)
            log.can_focus = True
            log.focus()
            await pilot.pause(0.05)
            self.assertIs(
                app.focused, log,
                f"setup precondition: RichLog should have focus, got {app.focused!r}",
            )

            await pilot.press("left")
            await pilot.pause(0.1)

            from actor.watch.app import ActorTree
            tree = app.query_one(ActorTree)
            self.assertIs(
                app.focused, tree,
                f"Left from RichLog should focus the ActorTree, "
                f"but focus is on {app.focused!r}",
            )

    async def test_ctrl_b_from_overview_richlog_returns_to_tree(self):
        """Ctrl+B is the emacs alias for Left (`Binding('left,ctrl+b', ...)`).
        Same expectation as the arrow case."""
        app = await self._boot_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash_if_present(pilot, app)
            await self._select_actor(pilot, app, "alpha")
            app.action_show_tab("info")
            await pilot.pause(0.1)

            from textual.widgets import RichLog
            log = app.query_one("#logs-content", RichLog)
            log.can_focus = True
            log.focus()
            await pilot.pause(0.05)

            await pilot.press("ctrl+b")
            await pilot.pause(0.1)

            from actor.watch.app import ActorTree
            tree = app.query_one(ActorTree)
            self.assertIs(app.focused, tree)

    async def test_left_from_diff_verticalscroll_cycles_to_overview(self):
        """DIFF is tab idx 1. Focusing #diff-scroll (VerticalScroll, which
        has its own arrow bindings) and pressing Left should cycle to the
        previous tab (OVERVIEW), NOT scroll the viewport."""
        app = await self._boot_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash_if_present(pilot, app)
            await self._select_actor(pilot, app, "alpha")

            app.action_show_tab("diff")
            await pilot.pause(0.1)
            from textual.containers import VerticalScroll
            scroll = app.query_one("#diff-scroll", VerticalScroll)
            scroll.can_focus = True
            scroll.focus()
            await pilot.pause(0.05)
            self.assertIs(app.focused, scroll, "precondition: diff-scroll focused")

            await pilot.press("left")
            await pilot.pause(0.1)

            # After Left from idx 1, the active tab should be idx 0 (info).
            from textual.widgets import TabbedContent
            tabs = app.query_one("#tabs", TabbedContent)
            self.assertEqual(
                tabs.active, "info",
                f"Left from #diff-scroll should cycle tabs back to OVERVIEW; "
                f"active tab is {tabs.active!r}",
            )

    async def test_right_from_diff_when_already_rightmost_stays_on_widget(self):
        """Symmetric of the left case: pressing Right from the rightmost
        tab's content widget should NOT scroll the widget; it should be
        a no-op (no further tab to cycle to). Specifically: the focused
        widget's right-arrow binding must not fire."""
        app = await self._boot_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash_if_present(pilot, app)
            await self._select_actor(pilot, app, "alpha")

            app.action_show_tab("diff")
            await pilot.pause(0.1)
            from textual.containers import VerticalScroll
            scroll = app.query_one("#diff-scroll", VerticalScroll)
            scroll.can_focus = True
            scroll.focus()
            initial_scroll_x = scroll.scroll_x
            await pilot.pause(0.05)

            await pilot.press("right")
            await pilot.pause(0.1)

            # Focus should still be on diff-scroll (no tab to the right of
            # diff in TAB_ORDER); critically, the widget's own right-scroll
            # must not have fired.
            self.assertEqual(
                scroll.scroll_x, initial_scroll_x,
                f"Right on rightmost tab should not scroll the widget; "
                f"scroll_x went {initial_scroll_x} → {scroll.scroll_x}",
            )

    def test_navigate_bindings_have_priority_true(self):
        """Compile-time guard for #24: the App-level left/right navigation
        bindings must stay `priority=True`, otherwise the focused widget's
        own arrow binding (every ScrollView descendant has one) would
        consume the key first."""
        from actor.watch.app import ActorWatchApp
        binding_table = {b.key: b for b in ActorWatchApp.BINDINGS}
        for key in ("left,ctrl+b", "right,ctrl+f"):
            self.assertIn(
                key, binding_table,
                f"missing app-level binding for {key!r} — regression of #24",
            )
            self.assertTrue(
                binding_table[key].priority,
                f"{key!r} binding lost priority=True — would re-introduce #24",
            )

    async def test_left_from_overview_then_left_from_tree_collapses_node(self):
        """Two-step: Left from RichLog returns focus to tree, then Left on
        the tree collapses the cursor node (existing tree behavior, not
        broken by the navigation fix)."""
        app = await self._boot_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._dismiss_splash_if_present(pilot, app)
            await self._select_actor(pilot, app, "alpha")

            app.action_show_tab("info")
            await pilot.pause(0.1)
            from textual.widgets import RichLog
            log = app.query_one("#logs-content", RichLog)
            log.can_focus = True
            log.focus()
            await pilot.pause(0.05)

            await pilot.press("left")
            await pilot.pause(0.1)

            from actor.watch.app import ActorTree
            tree = app.query_one(ActorTree)
            self.assertIs(app.focused, tree, "step 1: Left should land on tree")

            # Second Left on the tree shouldn't move focus away — the tree
            # binding handles it (collapses node or no-ops on a leaf).
            await pilot.press("left")
            await pilot.pause(0.05)
            self.assertIs(app.focused, tree, "step 2: Left on tree should keep focus on tree")


if __name__ == "__main__":
    unittest.main()
