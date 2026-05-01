"""Modal-overlay variant of Textual's HelpPanel.

The built-in `HelpPanel` docks to the right of the active screen via
`split: right`. We want the same dim-backdrop centred-card feel as the
command palette, so this module pushes a `ModalScreen` that holds the
KeyPanel-equivalent bindings table plus the focused widget's HELP
markdown (when set).

Bindings shown are pulled from the screen *underneath* the modal —
not from the modal itself — because rendering inside a ModalScreen
would otherwise show only the overlay's own dismiss bindings.
`_AppBindingsTable.render_bindings_table` swaps the source
accordingly.
"""
from __future__ import annotations

from collections import defaultdict
from textwrap import dedent

from rich import box
from rich.table import Table
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import SystemModalScreen
from textual.widget import Widget
from textual.widgets import Markdown
from textual.widgets._key_panel import BindingsTable


# Hand-curated panel layout. Each entry is one of:
#   ("action", action_string) — render the App.BINDINGS row whose
#       Binding.action matches `action_string`. Aggregates aliases
#       from the same action onto one line.
#   ("group", [action, ...], description) — combine the keys from
#       several actions into a single row labelled with
#       `description`. Used for the four arrow + emacs nav bindings,
#       which read as one concept ("Navigate") in the panel.
#   ("synthetic", keys, description) — a row for an action handled
#       outside App.BINDINGS (Enter on the tree, Ctrl+Z in the
#       terminal). Rendered by building a transient `Binding` so
#       `App.get_key_display` formats it consistently with the
#       real bindings.
#   None — blank spacer row.
_PANEL_LAYOUT: list[tuple | None] = [
    ("action", "command_palette"),
    ("action", "focus_actors"),
    ("action", "enter_interactive"),
    ("action", "show_tab('info')"),
    ("action", "show_tab('diff')"),
    None,
    ("synthetic", "enter", "Enter actor / focus terminal"),
    ("synthetic", "ctrl+z", "Leave the embedded terminal"),
    None,
    ("action", "navigate_left"),
    ("action", "navigate_right"),
    ("action", "navigate_up"),
    ("action", "navigate_down"),
    None,
    ("action", "quit"),
]


class _AppBindingsTable(BindingsTable):
    """A `BindingsTable` that renders a hand-curated, focus-stable
    keymap for actor.sh.

    The base `BindingsTable.render_bindings_table` walks
    `self.screen.active_bindings`, which varies by focus context
    (Tree adds expand/collapse, Tabs add cycle, the embedded
    terminal forwards almost everything). For an actor.sh user the
    "what can I do" surface is a fixed list — the global keymap
    defined on `App`, plus a small set of synthetic entries for
    keys handled outside `App.BINDINGS` (Enter on tree, Ctrl+Z in
    the terminal). The order matches `_PANEL_LAYOUT`."""

    def render_bindings_table(self) -> Table:
        key_style = self.get_component_rich_style("bindings-table--key")
        divider_transparent = (
            self.get_component_styles("bindings-table--divider").color.a == 0
        )
        table = Table(
            padding=(0, 0),
            show_header=False,
            box=box.SIMPLE if divider_transparent else box.HORIZONTALS,
            border_style=self.get_component_rich_style("bindings-table--divider"),
        )
        table.add_column("", justify="right")

        description_style = self.get_component_rich_style(
            "bindings-table--description"
        )

        def render_description(binding: Binding) -> Text:
            text = Text.from_markup(
                binding.description, end="", style=description_style
            )
            if binding.tooltip:
                if binding.description:
                    text.append(" ")
                text.append(binding.tooltip, "dim")
            return text

        # Index App.BINDINGS by action so layout entries can look up
        # bindings without re-walking the full list per item.
        # Multiple keys → one action collapses into a list of
        # `Binding` aliases (e.g. `left,ctrl+b` → two Bindings, one
        # per key, both with `action="navigate_left"`).
        action_to_bindings: defaultdict[str, list[Binding]] = defaultdict(list)
        for _, binding in self.app._bindings:
            if binding.system:
                continue
            action_to_bindings[binding.action].append(binding)

        get_key_display = self.app.get_key_display

        def keys_for(bindings: list[Binding]) -> str:
            # Preserve listing order while deduping aliases that
            # would render identically.
            return " ".join(
                dict.fromkeys(get_key_display(b) for b in bindings)
            )

        def add_row(keys: str, description: str | Text) -> None:
            if isinstance(description, Text):
                desc_text = description
            else:
                desc_text = Text.from_markup(
                    description, end="", style=description_style
                )
            table.add_row(Text(keys, style=key_style), desc_text)

        for entry in _PANEL_LAYOUT:
            if entry is None:
                table.add_row("", "")
                continue
            kind = entry[0]
            if kind == "action":
                _, action = entry
                bindings = action_to_bindings.get(action, [])
                if not bindings:
                    continue
                add_row(keys_for(bindings), render_description(bindings[0]))
            elif kind == "group":
                _, actions, description = entry
                bindings: list[Binding] = []
                for action in actions:
                    bindings.extend(action_to_bindings.get(action, []))
                if not bindings:
                    continue
                add_row(keys_for(bindings), description)
            elif kind == "synthetic":
                _, keys, description = entry
                synthetic = Binding(keys, "", description)
                add_row(get_key_display(synthetic), description)

        return table


class HelpOverlay(SystemModalScreen[None]):
    """Centred, dim-backdrop alternative to Textual's `HelpPanel`.

    Shows the focused widget's `HELP` markdown (when present) plus a
    bindings table for the active surface. Mirrors the command
    palette's modal feel — dim everything else, render the panel as
    a bordered card on top, dismiss with Escape or `?`.

    Extends `SystemModalScreen` (not `ModalScreen`) for the same
    reason `CommandPalette` does: it bypasses the app's own CSS
    (`inherit_css=False`). Without that bypass, app-level rules like
    `Screen { background: ansi_default }` override our dim backdrop
    and the overlay paints transparent against the terminal."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("question_mark", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    HelpOverlay {
        /* Same dim-backdrop the command palette uses — explicit
           rather than inherited from `ModalScreen` so themes that
           drop background alpha (or override it elsewhere) can't
           accidentally remove the darkening. */
        background: $background 60%;
        align: center middle;

        &:ansi {
            background: transparent;
        }
    }

    HelpOverlay > Vertical {
        width: 70%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }

    HelpOverlay #help-markdown {
        background: $surface;
        height: auto;
        max-height: 50%;
        margin-bottom: 1;
        padding: 0;
    }

    HelpOverlay #help-markdown.-empty {
        display: none;
    }

    HelpOverlay _AppBindingsTable {
        background: $surface;
        height: auto;

        & > .bindings-table--key {
            color: $text-accent;
            text-style: bold;
            padding: 0 1;
        }
        & > .bindings-table--description {
            color: $foreground;
        }
        & > .bindings-table--divider {
            color: transparent;
        }
        & > .bindings-table--header {
            color: $text-primary;
            text-style: underline;
        }
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Markdown(id="help-markdown")
            yield _AppBindingsTable()

    def on_mount(self) -> None:
        # Populate the focused-widget HELP markdown — same lookup the
        # built-in HelpPanel uses, but resolved against the screen
        # underneath us (the modal itself has no useful focus chain).
        markdown = self.query_one("#help-markdown", Markdown)
        help_text = self._collect_focused_help()
        if help_text:
            markdown.update(dedent(help_text.rstrip()))
            markdown.remove_class("-empty")
        else:
            markdown.add_class("-empty")

    def _collect_focused_help(self) -> str:
        stack = self.app.screen_stack
        if len(stack) < 2:
            return ""
        parent = stack[-2]
        focused = parent.focused
        if focused is None:
            return ""
        for node in focused.ancestors_with_self:
            if isinstance(node, Widget) and node.HELP:
                return node.HELP
        return ""
