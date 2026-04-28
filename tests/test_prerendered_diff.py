"""Tests for the off-thread renderable→Strips path.

The diff worker calls `renderable_to_strips` to push the CPU-bound
Segment-generation step out of the main thread. The mounted
`PrerenderedDiff` widget then serves those Strips via a constant-time
array index, so painting is effectively free per file.
"""
from __future__ import annotations

import unittest

from rich.segment import Segment
from rich.text import Text
from textual.geometry import Size
from textual.strip import Strip

from actor.watch.prerendered_diff import (
    PrerenderedDiff,
    renderable_to_strips,
)


class RenderableToStripsTests(unittest.TestCase):
    def test_zero_width_returns_empty_list(self):
        self.assertEqual(renderable_to_strips(Text("hi"), 0), [])

    def test_single_line_yields_one_strip(self):
        strips = renderable_to_strips(Text("hello"), 80)
        # One source line → one Strip. (Rich appends one trailing
        # \n in render output that becomes a separator, so we
        # tolerate either 1 or 2 strips depending on render shape;
        # the important thing is "hello" survives intact in the
        # first non-empty strip.)
        self.assertGreaterEqual(len(strips), 1)
        text = "".join(seg.text for seg in strips[0]).rstrip()
        self.assertIn("hello", text)

    def test_multiline_yields_multiple_strips(self):
        strips = renderable_to_strips(Text("line one\nline two\nline three"), 80)
        joined = [
            "".join(seg.text for seg in s).rstrip()
            for s in strips
        ]
        # Accept any non-empty trailing strip; primary content must
        # appear in order.
        non_empty = [line for line in joined if line]
        self.assertEqual(non_empty[:3], ["line one", "line two", "line three"])

    def test_styled_text_preserves_colors_in_segments(self):
        # Hex colors must survive — downgrading to ANSI-256 would
        # round to nearest, shifting the diff palette.
        styled = Text("colored", style="#ff0000 on #001122")
        strips = renderable_to_strips(styled, 80)
        # First strip should contain at least one segment with the
        # original color encoded.
        first = strips[0]
        styles = [seg.style for seg in first if seg.style is not None]
        self.assertTrue(
            any(
                s.color is not None and s.color.triplet is not None
                and (s.color.triplet.red, s.color.triplet.green,
                     s.color.triplet.blue) == (255, 0, 0)
                for s in styles
            ),
            f"expected (255, 0, 0) RGB in styles {styles!r}",
        )


class PrerenderedDiffWidgetTests(unittest.TestCase):
    """The widget's render-side contract: render_line returns the
    pre-baked strip; height matches strip count + 1 trailing blank."""

    def _make(self, n: int) -> PrerenderedDiff:
        strips = [Strip([Segment(f"line {i}")]) for i in range(n)]
        return PrerenderedDiff(strips)

    def test_get_content_height_matches_strip_count_plus_separator(self):
        w = self._make(5)
        # +1 for the trailing blank separator the widget appends.
        self.assertEqual(
            w.get_content_height(Size(100, 100), Size(100, 100), 100),
            6,
        )

    def test_render_line_returns_indexed_strip(self):
        w = self._make(3)
        strip = w.render_line(1)
        self.assertEqual(strip._segments[0].text, "line 1")

    def test_render_line_out_of_range_returns_blank(self):
        w = self._make(2)
        # Past the trailing separator → blank.
        strip = w.render_line(99)
        # Strip.blank produces a Strip whose text content is empty
        # (or whitespace) at the requested width.
        self.assertEqual("".join(seg.text for seg in strip).strip(), "")


if __name__ == "__main__":
    unittest.main()
