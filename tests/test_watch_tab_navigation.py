from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch
import unittest

from textual.widgets import Static, TabbedContent, TabPane, Tabs

from actor.types import Actor, ActorConfig, AgentKind, Status
from actor.watch.app import ActorWatchApp
from actor.watch.tree import ActorTree


def _actor(
    name: str = "alice",
    updated_at: str = "now",
    parent: str | None = None,
) -> Actor:
    return Actor(
        name=name,
        agent=AgentKind.CODEX,
        agent_session="session",
        dir=f"/tmp/{name}",
        source_repo=None,
        base_branch="main",
        worktree=False,
        parent=parent,
        config=ActorConfig(),
        created_at=updated_at,
        updated_at=updated_at,
    )


def _tree_click_event(line: int, *, toggle: bool = False) -> SimpleNamespace:
    meta = {"line": line}
    if toggle:
        meta["toggle"] = True

    def prevent_default() -> None:
        return None

    def stop() -> None:
        return None

    return SimpleNamespace(
        style=SimpleNamespace(meta=meta),
        prevent_default=prevent_default,
        stop=stop,
    )


@contextmanager
def _patched_ready(actor: Actor | list[Actor]):
    actors = actor if isinstance(actor, list) else [actor]
    patches = [
        patch.object(
            ActorWatchApp,
            "_fetch_actors",
            return_value=(actors, {item.name: Status.IDLE for item in actors}),
        ),
        patch.object(ActorWatchApp, "_try_apply_omarchy_theme", return_value=False),
        patch.object(ActorWatchApp, "_install_sigusr2_handler"),
        patch.object(ActorWatchApp, "_refresh_detail"),
        patch.object(ActorWatchApp, "_poll_actors_async"),
        patch.object(ActorWatchApp, "_poll_omarchy_theme"),
        patch.object(ActorWatchApp, "_poll_diff_for_running"),
        patch.object(ActorWatchApp, "_poll_diff_badge_for_selected"),
        patch.object(ActorWatchApp, "_tick_overview_age"),
        patch.object(ActorWatchApp, "_tick_overview_running_icon"),
    ]
    for item in patches:
        item.start()
    try:
        yield
    finally:
        for item in reversed(patches):
            item.stop()


class TabNavigationFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_leaves_inner_tabs_focusable(self):
        actor = _actor()
        app = ActorWatchApp(animate=False)
        with _patched_ready(actor):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)
                tabs_bar = app.query_one("#tabs", TabbedContent).query_one(Tabs)
                self.assertTrue(tabs_bar.can_focus)

    async def test_right_from_tree_to_interactive_focuses_tab_bar_now(self):
        actor = _actor()
        app = ActorWatchApp(animate=False)
        with _patched_ready(actor):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)
                tabbed = app.query_one("#tabs", TabbedContent)
                await tabbed.add_pane(
                    TabPane(
                        "INTERACTIVE",
                        Static("terminal placeholder", id="fake-terminal"),
                        id="interactive",
                    ),
                    before="info",
                )
                tabbed.active = "interactive"
                await pilot.pause(0.05)

                tree = app.query_one(ActorTree)
                app.set_focus(tree, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)

                await pilot.press("right")
                await pilot.pause(0.05)

                tabs_bar = tabbed.query_one(Tabs)
                self.assertIs(app.focused, tabs_bar)
                label = str(tabbed.get_tab("interactive").label)
                self.assertEqual(label, "→ INTERACTIVE")

    async def test_terminal_exit_request_focuses_tab_bar(self):
        actor = _actor()
        app = ActorWatchApp(animate=False)
        with _patched_ready(actor):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)
                tabbed = app.query_one("#tabs", TabbedContent)
                await tabbed.add_pane(
                    TabPane(
                        "INTERACTIVE",
                        Static("terminal placeholder", id="fake-terminal"),
                        id="interactive",
                    ),
                    before="info",
                )
                tabbed.active = "interactive"
                await pilot.pause(0.05)

                fake_terminal = app.query_one("#fake-terminal", Static)
                fake_terminal.can_focus = True
                app.set_focus(fake_terminal, scroll_visible=False)
                await pilot.pause(0.05)
                self.assertIs(app.focused, fake_terminal)

                app.on_terminal_widget_exit_requested(object())
                await pilot.pause(0.05)

                tabs_bar = tabbed.query_one(Tabs)
                self.assertIs(app.focused, tabs_bar)
                self.assertEqual(
                    str(tabbed.get_tab("interactive").label),
                    "→ INTERACTIVE",
                )

    async def test_enter_interactive_focuses_terminal_widget(self):
        actor = _actor()
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.return_value = SimpleNamespace(widget=fake_terminal)
        app._interactive = manager

        with _patched_ready(actor):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                app.action_enter_interactive()
                for _ in range(20):
                    if app.focused is fake_terminal:
                        break
                    await pilot.pause(0.02)

                tabbed = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabbed.active, "interactive")
                self.assertIs(app.focused, fake_terminal)

    async def test_enter_on_interactive_tab_bar_focuses_terminal_widget(self):
        actor = _actor()
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.return_value = SimpleNamespace(widget=fake_terminal)
        app._interactive = manager

        with _patched_ready(actor):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                app._sync_detail_view()
                await pilot.pause(0.1)

                tabbed = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabbed.active, "interactive")
                tabs_bar = tabbed.query_one(Tabs)
                app.set_focus(tabs_bar, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)

                await pilot.press("enter")
                for _ in range(20):
                    if app.focused is fake_terminal:
                        break
                    await pilot.pause(0.02)

                self.assertIs(app.focused, fake_terminal)

    async def test_enter_on_collapsed_actor_expands_before_interactive(self):
        alice = _actor("alice", updated_at="2026-04-30T02:00:00")
        bob = _actor(
            "bob",
            updated_at="2026-04-30T01:00:00",
            parent="alice",
        )
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.side_effect = (
            lambda name: SimpleNamespace(widget=fake_terminal)
            if name == "alice"
            else None
        )
        app._interactive = manager

        with _patched_ready([alice, bob]):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                tree = app.query_one(ActorTree)
                self.assertEqual(tree.selected_actor.name, "alice")
                self.assertFalse(tree.cursor_node.is_expanded)

                await pilot.press("enter")
                await pilot.pause(0.1)

                self.assertTrue(tree.cursor_node.is_expanded)
                self.assertIs(app.focused, tree)
                manager.has.assert_not_called()

                await pilot.press("enter")
                for _ in range(20):
                    if app.focused is fake_terminal:
                        break
                    await pilot.pause(0.02)

                tabbed = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabbed.active, "interactive")
                self.assertIs(app.focused, fake_terminal)

    async def test_click_actor_selects_without_entering_interactive(self):
        alice = _actor("alice", updated_at="2026-04-30T02:00:00")
        bob = _actor("bob", updated_at="2026-04-30T01:00:00")
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.side_effect = (
            lambda name: SimpleNamespace(widget=fake_terminal)
            if name == "alice"
            else None
        )
        app._interactive = manager

        with _patched_ready([alice, bob]):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                tree = app.query_one(ActorTree)
                tabbed = app.query_one("#tabs", TabbedContent)
                app.action_show_tab("info")
                app.set_focus(tree, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)
                bob_node = next(
                    node for node in tree.root.children
                    if node.data is not None and node.data.name == "bob"
                )

                await pilot.click(tree, offset=(4, bob_node._line))
                await pilot.pause(0.1)

                self.assertEqual(tree.selected_actor.name, "bob")
                self.assertIs(app.focused, tree)
                self.assertEqual(tabbed.active, "info")

                alice_node = next(
                    node for node in tree.root.children
                    if node.data is not None and node.data.name == "alice"
                )
                await pilot.click(tree, offset=(4, alice_node._line))
                await pilot.pause(0.1)

                self.assertEqual(tree.selected_actor.name, "alice")
                self.assertEqual(tabbed.active, "info")
                self.assertIsNot(app.focused, fake_terminal)
                manager.has.assert_not_called()
                manager.create.assert_not_called()

    async def test_click_back_to_interactive_actor_restores_interactive_tab(self):
        alice = _actor("alice", updated_at="2026-04-30T02:00:00")
        bob = _actor("bob", updated_at="2026-04-30T01:00:00")
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.side_effect = (
            lambda name: SimpleNamespace(widget=fake_terminal)
            if name == "alice"
            else None
        )
        app._interactive = manager

        with _patched_ready([alice, bob]):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                tree = app.query_one(ActorTree)
                tabbed = app.query_one("#tabs", TabbedContent)
                app._sync_detail_view()
                await pilot.pause(0.1)
                self.assertEqual(tabbed.active, "interactive")

                app.set_focus(tree, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)

                bob_node = next(
                    node for node in tree.root.children
                    if node.data is not None and node.data.name == "bob"
                )
                await pilot.click(tree, offset=(4, bob_node._line))
                await pilot.pause(0.1)

                self.assertEqual(tree.selected_actor.name, "bob")
                self.assertNotEqual(tabbed.active, "interactive")

                alice_node = next(
                    node for node in tree.root.children
                    if node.data is not None and node.data.name == "alice"
                )
                await pilot.click(tree, offset=(4, alice_node._line))
                for _ in range(20):
                    if tabbed.active == "interactive":
                        break
                    await pilot.pause(0.02)

                self.assertEqual(tree.selected_actor.name, "alice")
                self.assertEqual(tabbed.active, "interactive")
                self.assertIsNot(app.focused, fake_terminal)
                manager.has.assert_not_called()
                manager.create.assert_not_called()

    async def test_click_interactive_actor_reuses_existing_interactive_tab(self):
        alice = _actor("alice", updated_at="2026-04-30T02:00:00")
        bob = _actor("bob", updated_at="2026-04-30T01:00:00")
        app = ActorWatchApp(animate=False)
        alice_terminal = Static("alice terminal", id="alice-terminal")
        alice_terminal.can_focus = True
        bob_terminal = Static("bob terminal", id="bob-terminal")
        bob_terminal.can_focus = True
        sessions = {
            "alice": SimpleNamespace(widget=alice_terminal),
            "bob": SimpleNamespace(widget=bob_terminal),
        }
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.side_effect = lambda name: sessions.get(name)
        app._interactive = manager

        with _patched_ready([alice, bob]):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                tree = app.query_one(ActorTree)
                tabbed = app.query_one("#tabs", TabbedContent)
                app._sync_detail_view()
                await pilot.pause(0.1)

                self.assertIn(
                    alice_terminal,
                    list(tabbed.get_pane("interactive").children),
                )
                tabbed.active = "info"
                app.set_focus(tree, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)

                bob_node = next(
                    node for node in tree.root.children
                    if node.data is not None and node.data.name == "bob"
                )
                await pilot.click(tree, offset=(4, bob_node._line))

                for _ in range(20):
                    if bob_terminal in list(tabbed.get_pane("interactive").children):
                        break
                    await pilot.pause(0.02)

                self.assertEqual(tree.selected_actor.name, "bob")
                self.assertEqual(tabbed.active, "info")
                self.assertIn(
                    bob_terminal,
                    list(tabbed.get_pane("interactive").children),
                )
                self.assertNotIn(
                    alice_terminal,
                    list(tabbed.get_pane("interactive").children),
                )
                self.assertIsNot(app.focused, bob_terminal)
                manager.has.assert_not_called()
                manager.create.assert_not_called()

    async def test_click_collapsed_actor_does_not_expand_or_enter_interactive(self):
        alice = _actor("alice", updated_at="2026-04-30T02:00:00")
        bob = _actor(
            "bob",
            updated_at="2026-04-30T01:00:00",
            parent="alice",
        )
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.return_value = SimpleNamespace(widget=fake_terminal)
        app._interactive = manager

        with _patched_ready([alice, bob]):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                tree = app.query_one(ActorTree)
                self.assertEqual(tree.selected_actor.name, "alice")
                self.assertFalse(tree.cursor_node.is_expanded)

                await tree._on_click(_tree_click_event(tree.cursor_node._line))
                await pilot.pause(0.1)

                self.assertFalse(tree.cursor_node.is_expanded)
                self.assertIs(app.focused, tree)
                manager.has.assert_not_called()
                manager.create.assert_not_called()

    async def test_clicking_away_from_interactive_cancels_pending_terminal_focus(self):
        actor = _actor()
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.return_value = True
        manager.get.return_value = SimpleNamespace(widget=fake_terminal)
        app._interactive = manager

        with _patched_ready(actor):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                app._sync_detail_view()
                await pilot.pause(0.1)
                tabbed = app.query_one("#tabs", TabbedContent)
                self.assertEqual(tabbed.active, "interactive")

                app._pending_interactive_focus = fake_terminal
                tabbed.active = "info"
                await pilot.pause(0.1)

                self.assertIsNone(app._pending_interactive_focus)
                self.assertIsNot(app.focused, fake_terminal)

                app._pending_interactive_focus = fake_terminal
                app._on_tabs_click(object())
                await pilot.pause(0.1)

                self.assertIsNone(app._pending_interactive_focus)
                self.assertIsNot(app.focused, fake_terminal)

    async def test_left_from_interactive_tab_bar_returns_to_actors(self):
        actor = _actor()
        app = ActorWatchApp(animate=False)
        with _patched_ready(actor):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)
                tabbed = app.query_one("#tabs", TabbedContent)
                await tabbed.add_pane(
                    TabPane(
                        "INTERACTIVE",
                        Static("terminal placeholder", id="fake-terminal"),
                        id="interactive",
                    ),
                    before="info",
                )
                tabbed.active = "interactive"
                await pilot.pause(0.05)

                tabs_bar = tabbed.query_one(Tabs)
                app.set_focus(tabs_bar, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)

                await pilot.press("left")
                await pilot.pause(0.05)

                self.assertIs(app.focused, app.query_one(ActorTree))
                self.assertEqual(tabbed.active, "interactive")

    async def test_actor_switch_removing_interactive_keeps_tree_focus(self):
        alice = _actor("alice", updated_at="2026-04-30T02:00:00")
        bob = _actor("bob", updated_at="2026-04-30T01:00:00")
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.side_effect = lambda name: name == "alice"
        manager.get.side_effect = (
            lambda name: SimpleNamespace(widget=fake_terminal)
            if name == "alice"
            else None
        )
        app._interactive = manager

        with _patched_ready([alice, bob]):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                tree = app.query_one(ActorTree)
                self.assertEqual(tree.selected_actor.name, "alice")

                app._sync_detail_view()
                await pilot.pause(0.1)
                tabbed = app.query_one("#tabs", TabbedContent)
                tabbed.active = "interactive"
                app.set_focus(tree, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)

                await pilot.press("down")
                await pilot.pause(0.1)

                self.assertEqual(tree.selected_actor.name, "bob")
                self.assertIs(app.focused, tree)
                self.assertNotEqual(tabbed.active, "interactive")

                await pilot.press("up")
                await pilot.pause(0.05)

                self.assertEqual(tree.selected_actor.name, "alice")
                self.assertIs(app.focused, tree)
                self.assertEqual(tabbed.active, "interactive")

    async def test_actor_switch_preserves_diff_when_interactive_available(self):
        alice = _actor("alice", updated_at="2026-04-30T02:00:00")
        bob = _actor("bob", updated_at="2026-04-30T01:00:00")
        app = ActorWatchApp(animate=False)
        fake_terminal = Static("terminal placeholder", id="fake-terminal")
        fake_terminal.can_focus = True
        manager = MagicMock()
        manager.live_names.return_value = []
        manager.has.side_effect = lambda name: name == "alice"
        manager.get.side_effect = (
            lambda name: SimpleNamespace(widget=fake_terminal)
            if name == "alice"
            else None
        )
        app._interactive = manager

        with _patched_ready([alice, bob]):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.1)

                tree = app.query_one(ActorTree)
                tabbed = app.query_one("#tabs", TabbedContent)
                app._sync_detail_view()
                await pilot.pause(0.1)
                tabbed.active = "diff"
                app.set_focus(tree, scroll_visible=False)
                app._refresh_focus_indicators()
                await pilot.pause(0.05)

                await pilot.press("down")
                await pilot.pause(0.1)
                self.assertEqual(tree.selected_actor.name, "bob")
                self.assertIs(app.focused, tree)
                self.assertEqual(tabbed.active, "diff")

                await pilot.press("up")
                await pilot.pause(0.1)
                self.assertEqual(tree.selected_actor.name, "alice")
                self.assertIs(app.focused, tree)
                self.assertEqual(tabbed.active, "diff")
