"""Actor tree widget."""

from __future__ import annotations

from textual.widgets import Tree

from ..types import Actor, Status
from .helpers import STATUS_ICON, group_by_parent


class ActorTree(Tree[Actor]):
    """Left panel showing all actors as a tree."""

    DEFAULT_CSS = """
    ActorTree {
        width: 28;
        border: blank;
        padding: 0 1;
    }
    ActorTree:focus {
        border: round $primary;
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
        self._snapshot: dict[str, str] = {}

    def update_actors(self, actors: list[Actor], statuses: dict[str, Status]) -> None:
        new_snapshot: dict[str, str] = {}
        for a in actors:
            status = statuses.get(a.name, Status.IDLE)
            icon = STATUS_ICON.get(status, "?")
            new_snapshot[a.name] = f"{icon} {a.name}"

        if new_snapshot == self._snapshot:
            return

        if set(new_snapshot.keys()) == set(self._snapshot.keys()):
            self._update_labels(self.root, new_snapshot)
            self._snapshot = new_snapshot
            return

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
                label = new_snapshot[actor.name]
                has_children = actor.name in by_parent
                if has_children:
                    should_expand = actor.name in expanded or actor.name not in self._snapshot
                    node = parent_node.add(label, data=actor, expand=should_expand)
                    _add_children(node, actor.name)
                else:
                    parent_node.add_leaf(label, data=actor)

        _add_children(self.root, None)

        if selected_name:
            self._select_by_name(self.root, selected_name)
        elif self.root.children:
            self.select_node(self.root.children[0])

    def _update_labels(self, node, snapshot: dict[str, str]) -> None:
        for child in node.children:
            if child.data and child.data.name in snapshot:
                new_label = snapshot[child.data.name]
                if str(child.label) != new_label:
                    child.set_label(new_label)
            self._update_labels(child, snapshot)

    def _collect_expanded(self, node, expanded: set[str]) -> None:
        for child in node.children:
            if child.data and child.is_expanded:
                expanded.add(child.data.name)
            self._collect_expanded(child, expanded)

    def _select_by_name(self, node, name: str) -> bool:
        for child in node.children:
            if child.data and child.data.name == name:
                self.select_node(child)
                return True
            if self._select_by_name(child, name):
                return True
        return False

    @property
    def selected_actor(self) -> Actor | None:
        node = self.cursor_node
        if node and node.data:
            return node.data
        return None
