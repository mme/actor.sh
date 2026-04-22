"""Actor tree widget."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

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
    ActorTree > .tree--cursor {
        background: $primary;
        color: $text;
        text-style: bold;
    }
    """

    def __init__(self) -> None:
        super().__init__("Actors", id="actor-tree")
        self.show_root = False
        self.guide_depth = 3
        self._snapshot: dict[str, Status] = {}
        self._statuses: dict[str, Status] = {}
        self._anim_frame: int = 0

    def on_mount(self) -> None:
        self.set_interval(0.5, self._tick_animation)

    def render_label(
        self, node: TreeNode, base_style: Style, style: Style
    ) -> Text:
        """Add a `→` prefix to the currently-selected actor row so
        the user always knows which actor is active in the detail
        pane. Non-selected actor rows get a space of the same width
        so row alignment doesn't shift when the cursor moves."""
        label = super().render_label(node, base_style, style)
        if node.data is None:
            return label  # group / root nodes — no indicator
        arrow = "→" if node is self.cursor_node else " "
        return Text(arrow, style=style) + label

    def _make_label(self, name: str, status: Status) -> str:
        """Generate display label for an actor."""
        if status == Status.RUNNING:
            icon = RUNNING_FRAMES[self._anim_frame]
        else:
            icon = STATUS_ICON.get(status, "")
        if icon:
            return f" {icon} {name} "
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
