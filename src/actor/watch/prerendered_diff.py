"""Off-thread Rich renderable → Strips → cheap widget mount.

The diff worker builds Rich renderables (Tables / Groups) per file off
the main thread. Without this module, those renderables would have
their `__rich_console__` invoked at *paint* time on the main thread to
generate Segments — a CPU-bound step that can take hundreds of ms
for a large diff and shows up as UI hangs on tab activation.

This module pushes the Segment-generation work into the worker too:

- `renderable_to_strips(renderable, width)` runs Rich's render pipeline
  and converts the output into a list of `textual.strip.Strip`
  objects. Safe to call from any thread; touches no widget state.
- `PrerenderedDiff` is a tiny widget that holds the resulting Strips
  and serves them via `render_line(y)` as a constant-time array
  lookup. Mounting it is cheap; painting it is cheap; layout sees a
  fixed height equal to the strip count.

Width is captured at kick time and threaded through. Stage 1's cache
key already includes content_width, so a real terminal resize kicks a
new build with strips at the new width — no need for the widget to
re-render on resize.
"""
from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.segment import Segment
from textual.geometry import Size
from textual.strip import Strip
from textual.widget import Widget


def renderable_to_strips(renderable: object, width: int) -> list[Strip]:
    """Render a Rich renderable into a list of Textual `Strip`s at the
    given width.

    Forces truecolor + a wide-enough output so hex-color styles
    emitted by the diff renderer survive intact (downgrading to
    ANSI-256 would lose precision and shift the diff palette away
    from the actor.sh theme).

    The width passed here is the scroll's content width at kick time.
    Slight overshoot (because we don't subtract the scrollbar
    column) is harmless — Textual's render pipeline crops Strips to
    the widget's actual width. Undershoot would be visible as a
    shorter-than-needed line, but VerticalScroll's children are
    handed the full inner width by default.
    """
    if width <= 0:
        return []
    console = Console(
        width=width,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        file=StringIO(),
        record=False,
    )
    options = console.options.update(width=width)
    segments = list(console.render(renderable, options))
    return [Strip(line) for line in Segment.split_lines(segments)]


class PrerenderedDiff(Widget):
    """Mounts a per-file diff that's already been converted to Strips.

    Worker thread does the expensive work (Rich Table → Segments →
    Strips); on the main thread, `render_line(y)` is just an array
    index. Height is fixed to the strip count so VerticalScroll can
    layout without invoking the renderable.

    A trailing blank Strip is appended at construction time so files
    have visual separation in the scroll (matches the pre-Stage-6
    `Static(Group(renderable, Text("")))` layout).
    """

    DEFAULT_CSS = """
    PrerenderedDiff {
        height: auto;
        width: 1fr;
        background: $background;
    }
    """
    # Concrete `$background` (not `ansi_default`) so Textual's dim-baking
    # filter has an RGB triplet to mix against. Pre-rendered strips
    # carry `dim=True` flags from Rich without a baked-in bgcolor —
    # `ansi_default` resolves to Rich's DEFAULT color (no triplet) and
    # the dim_color() pass crashes when it tries to unpack it.

    def __init__(self, strips: list[Strip], **kwargs) -> None:
        super().__init__(**kwargs)
        # Trailing blank line for inter-file spacing.
        self._strips = list(strips) + [Strip([])]

    def get_content_height(
        self, container: Size, viewport: Size, width: int
    ) -> int:
        return len(self._strips)

    def render_line(self, y: int) -> Strip:
        if 0 <= y < len(self._strips):
            return self._strips[y]
        return Strip.blank(self.size.width)
