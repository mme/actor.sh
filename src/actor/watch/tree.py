"""Actor tree widget."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

from textual import events
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from ..types import Actor, Status
from .helpers import STATUS_ICON, group_by_parent


RUNNING_FRAMES = ["♤", "♡", "♢", "♧"]


class ActorTree(Tree[Actor]):
    """Left panel showing all actors as a tree."""

    DEFAULT_CSS = """
    ActorTree {
        width: 1fr;
    }
    /* Dim the cursor highlight when the tree isn't focused — same
       $foreground 30% tint we use for the scrollbar track, panel
       underlines, and other "inactive" indicators. Saturates to
       $primary when the tree gains focus. Text stays the theme's
       standard foreground rather than $text (which auto-picks
       contrast and ends up bright white against our flavored bg). */
    ActorTree > .tree--cursor {
        background: $foreground 30%;
        color: $foreground;
        text-style: bold;
    }
    ActorTree:focus > .tree--cursor,
    ActorTree.-focus-active > .tree--cursor {
        background: $primary;
    }
    """

    def __init__(self) -> None:
        super().__init__("Actors", id="actor-tree")
        self.auto_expand = False
        self.show_root = False
        self.guide_depth = 3
        self._snapshot: dict[str, Status] = {}
        self._statuses: dict[str, Status] = {}
        self._anim_frame: int = 0
        self._highlight_from_mouse = False

    def on_mount(self) -> None:
        self.set_interval(0.5, self._tick_animation)

    def render_label(
        self, node: TreeNode, base_style: Style, style: Style
    ) -> Text:
        """Add a `→` prefix only to the currently-selected actor row
        while the tree has focus. All other rows (and every row when
        focus is elsewhere) get no prefix — so when the tree is
        unfocused the whole column shifts left by one char, which is
        the desired visual."""
        label = super().render_label(node, base_style, style)
        if node.data is None:
            return label  # group / root nodes — no indicator
        try:
            is_focused = self.screen.focused is self
        except Exception:
            is_focused = self.has_focus
        if node is self.cursor_node and is_focused:
            return Text("→", style=style) + label
        return label

    def on_focus(self) -> None:
        # Re-render so the arrow appears on the cursor row.
        self.refresh()

    def on_blur(self) -> None:
        # Re-render so the arrow disappears.
        self.refresh()

    async def _on_click(self, event: events.Click) -> None:
        """Mouse clicks select/highlight rows only.

        Textual's default Tree click handler runs the same
        `select_cursor` action as Enter, which makes a plain actor
        click indistinguishable from keyboard activation. Keep the
        default disclosure-triangle toggle behavior, but do not post
        NodeSelected for label clicks; the App treats NodeSelected as
        an explicit Enter activation.
        """
        async with self.lock:
            meta = event.style.meta
            if "line" not in meta:
                return
            event.prevent_default()
            event.stop()
            cursor_line = meta["line"]
            if meta.get("toggle", False):
                node = self.get_node_at_line(cursor_line)
                if node is not None:
                    self._toggle_node(node)
                return
            previous_line = self.cursor_line
            self._highlight_from_mouse = cursor_line != previous_line
            self.cursor_line = cursor_line
            if cursor_line == previous_line:
                self._highlight_from_mouse = False

    def consume_mouse_highlight(self) -> bool:
        from_mouse = self._highlight_from_mouse
        self._highlight_from_mouse = False
        return from_mouse

    def _make_label(self, name: str, status: Status) -> str:
        """Generate display label for an actor. Status icon (running
        animation / error glyph / etc.) sits to the RIGHT of the name
        — the name column stays flush left so a glance scans names
        without skipping over a leading icon column."""
        if status == Status.RUNNING:
            icon = RUNNING_FRAMES[self._anim_frame]
        else:
            icon = STATUS_ICON.get(status, "")
        if icon:
            return f" {name} {icon} "
        return f" {name} "

    # -- Animation -------------------------------------------------------------

    def _tick_animation(self) -> None:
        """Advance the running animation frame and update labels."""
        has_running = any(s == Status.RUNNING for s in self._statuses.values())
        if not has_running:
            return
        self._anim_frame = (self._anim_frame + 1) % len(RUNNING_FRAMES)
        self._update_animated_labels(self.root)

    def _update_animated_labels(self, node) -> None:
        """Update only running actor labels with the current animation frame."""
        for child in node.children:
            if child.data and self._statuses.get(child.data.name) == Status.RUNNING:
                new_label = self._make_label(child.data.name, Status.RUNNING)
                if str(child.label) != new_label:
                    child.set_label(new_label)
            self._update_animated_labels(child)

    # -- Data update -----------------------------------------------------------

    def update_actors(self, actors: list[Actor], statuses: dict[str, Status]) -> None:
        self._statuses = statuses

        new_snapshot = {a.name: statuses.get(a.name, Status.IDLE) for a in actors}

        if new_snapshot == self._snapshot:
            return

        if set(new_snapshot.keys()) == set(self._snapshot.keys()):
            # Same actors, status changed — update labels in place
            self._refresh_all_labels(self.root)
            self._snapshot = new_snapshot
            return

        # Structure changed — full rebuild
        selected_name = None
        if self.cursor_node and self.cursor_node.data:
            selected_name = self.cursor_node.data.name

        expanded: set[str] = set()
        self._collect_expanded(self.root, expanded)

        self.clear()
        self._snapshot = new_snapshot
        by_parent = group_by_parent(actors, statuses)
        visited: set[str] = set()

        def _add_children(parent_node, parent_key: str | None) -> None:
            for actor in by_parent.get(parent_key, []):
                if actor.name in visited:
                    continue
                visited.add(actor.name)
                status = statuses.get(actor.name, Status.IDLE)
                label = self._make_label(actor.name, status)
                has_children = actor.name in by_parent
                if has_children:
                    should_expand = actor.name in expanded
                    node = parent_node.add(label, data=actor, expand=should_expand)
                    _add_children(node, actor.name)
                else:
                    parent_node.add_leaf(label, data=actor)

        _add_children(self.root, None)

        if selected_name:
            self._move_cursor_by_name(self.root, selected_name)
        elif self.root.children:
            self.move_cursor(self.root.children[0])

    def _refresh_all_labels(self, node) -> None:
        """Update all labels based on current statuses."""
        for child in node.children:
            if child.data:
                status = self._statuses.get(child.data.name, Status.IDLE)
                new_label = self._make_label(child.data.name, status)
                if str(child.label) != new_label:
                    child.set_label(new_label)
            self._refresh_all_labels(child)

    def _collect_expanded(self, node, expanded: set[str]) -> None:
        for child in node.children:
            if child.data and child.is_expanded:
                expanded.add(child.data.name)
            self._collect_expanded(child, expanded)

    def _move_cursor_by_name(self, node, name: str) -> bool:
        for child in node.children:
            if child.data and child.data.name == name:
                self.move_cursor(child)
                return True
            if self._move_cursor_by_name(child, name):
                return True
        return False

    @property
    def selected_actor(self) -> Actor | None:
        node = self.cursor_node
        if node and node.data:
            return node.data
        return None
