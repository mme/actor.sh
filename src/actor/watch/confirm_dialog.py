"""Modal yes/no confirmation dialog for destructive palette actions.

Mirrors the dim-backdrop / centred-card feel of `HelpOverlay` and the
command palette. Subclasses `SystemModalScreen` (not `ModalScreen`)
for the same reason `CommandPalette` does — `inherit_css=False`
bypasses the actor.sh `Screen { background: ansi_default }` rule
that would otherwise erase the dim backdrop.

Use via `App.push_screen(ConfirmDialog(...), callback)`; the screen
returns `True` on confirm and `False` on cancel/Escape, dispatched
to the callback.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import SystemModalScreen
from textual.widgets import Button, Static


class ConfirmDialog(SystemModalScreen[bool]):
    """Yes/no confirmation dialog. Dismisses with `True` (confirm)
    or `False` (cancel). Defaults: Y/Enter confirms, N/Escape
    cancels — same shortcut shape as `git`'s interactive
    confirmations and `gh`'s prompts."""

    # Same mechanism `CommandPalette` uses to grab focus when the
    # screen is pushed. Without this the underlying screen's focus
    # is preserved, and `Enter` would activate whatever tab/widget
    # was focused before the dialog opened — defeating the
    # confirmation gate.
    AUTO_FOCUS = "#confirm"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("n", "cancel", "Cancel", show=False),
        Binding("y", "confirm", "Confirm", show=False),
        Binding("enter", "confirm", "Confirm", show=False),
        # Arrow keys cycle focus between Cancel ↔ Confirm. The App's
        # `navigate_*` priority bindings opt out via `check_action`
        # while a `SystemModalScreen` is on top, so by the time the
        # key falls through to the dialog these bindings handle it.
        Binding("left", "app.focus_previous", show=False),
        Binding("up", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
        Binding("down", "app.focus_next", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmDialog {
        background: $background 60%;
        align: center middle;

        &:ansi {
            background: transparent;
        }
    }

    ConfirmDialog > Vertical {
        width: auto;
        max-width: 70;
        height: auto;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }

    ConfirmDialog #title {
        text-style: bold;
        margin-bottom: 1;
        padding: 0;
    }

    ConfirmDialog #message {
        margin-bottom: 1;
        padding: 0;
    }

    ConfirmDialog #buttons {
        height: 3;
        align-horizontal: right;
    }

    ConfirmDialog #buttons Button {
        margin-left: 1;
        min-width: 10;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        message: str,
        confirm_label: str = "Confirm",
        confirm_variant: str = "primary",
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label
        self._confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, id="title", markup=False)
            yield Static(self._message, id="message", markup=False)
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    self._confirm_label,
                    id="confirm",
                    variant=self._confirm_variant,
                )

    def on_mount(self) -> None:
        # Auto-focus the confirm button so Enter triggers it via
        # default Button activation; the explicit `enter` binding is
        # belt-and-suspenders for keyboards where Enter doesn't reach
        # the focused Button (e.g. when focus wanders).
        try:
            self.query_one("#confirm", Button).focus()
        except Exception:
            pass

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")
