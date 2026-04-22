"""Omarchy palette → Textual theme mapping."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from actor.watch.omarchy_theme import (
    OMARCHY_THEME_NAME,
    _is_dark,
    _shift_toward_foreground,
    load_omarchy_theme,
    omarchy_colors_path,
    omarchy_theme_mtime,
)


def _write_palette(home: Path, body: str) -> Path:
    target = home / ".config" / "omarchy" / "current" / "theme" / "colors.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


_DARK_PALETTE = """\
accent = "#7aa2f7"
foreground = "#a9b1d6"
background = "#1a1b26"
color0 = "#32344a"
color1 = "#f7768e"
color2 = "#9ece6a"
color3 = "#e0af68"
color4 = "#7aa2f7"
color5 = "#ad8ee6"
color6 = "#449dab"
color7 = "#787c99"
"""

_LIGHT_PALETTE = """\
accent = "#2e5aa0"
foreground = "#222222"
background = "#f5f5f5"
color0 = "#e0e0e0"
color1 = "#a81829"
color2 = "#356c01"
color3 = "#8a6800"
color5 = "#721dbd"
"""


class TestLoadOmarchyTheme(unittest.TestCase):

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(load_omarchy_theme(home=Path(home)))

    def test_dark_palette_maps_to_theme(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), _DARK_PALETTE)
            theme = load_omarchy_theme(home=Path(home))
        self.assertIsNotNone(theme)
        assert theme is not None
        self.assertEqual(theme.name, OMARCHY_THEME_NAME)
        self.assertEqual(theme.primary, "#7aa2f7")
        self.assertEqual(theme.accent, "#7aa2f7")
        self.assertEqual(theme.secondary, "#ad8ee6")  # color5
        self.assertEqual(theme.error, "#f7768e")      # color1
        self.assertEqual(theme.success, "#9ece6a")    # color2
        self.assertEqual(theme.warning, "#e0af68")    # color3
        self.assertEqual(theme.foreground, "#a9b1d6")
        self.assertEqual(theme.background, "#1a1b26")
        self.assertEqual(theme.panel, "#32344a")      # color0
        self.assertTrue(theme.dark)

    def test_light_palette_detects_dark_false(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), _LIGHT_PALETTE)
            theme = load_omarchy_theme(home=Path(home))
        assert theme is not None
        self.assertFalse(theme.dark)

    def test_malformed_toml_returns_none_and_warns(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), "this = isn't valid TOML\nno no no")
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = load_omarchy_theme(home=Path(home))
        self.assertIsNone(result)
        self.assertIn("warning", buf.getvalue().lower())

    def test_missing_key_falls_back_silently(self):
        # A palette with only the strictly-required-to-render keys; the
        # rest should fall back to safe defaults without raising.
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), 'background = "#000000"\nforeground = "#ffffff"\n')
            theme = load_omarchy_theme(home=Path(home))
        self.assertIsNotNone(theme)
        assert theme is not None
        # Fallback accent (from _DARK_PALETTE's typical tokyonight hue).
        self.assertEqual(theme.accent, "#7aa2f7")

    def test_non_string_value_rejected_as_warning(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), "background = 42\n")
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = load_omarchy_theme(home=Path(home))
        self.assertIsNone(result)
        self.assertIn("malformed", buf.getvalue().lower())

    def test_hex_without_hash_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), 'background = "1a1b26"\n')
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = load_omarchy_theme(home=Path(home))
        self.assertIsNone(result)

    def test_three_digit_hex_normalized_to_six(self):
        # Spot-check via the internal helper — exposed for this exact
        # reason. Short-form hex is expanded to full-form for Textual.
        from actor.watch.omarchy_theme import _hex
        out = _hex({"k": "#abc"}, "k", "#000000")
        self.assertEqual(out, "#aabbcc")


class TestOmarchyThemeMtime(unittest.TestCase):

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(omarchy_theme_mtime(home=Path(home)))

    def test_present_file_returns_float(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), _DARK_PALETTE)
            mtime = omarchy_theme_mtime(home=Path(home))
        self.assertIsNotNone(mtime)
        assert mtime is not None
        self.assertGreater(mtime, 0)

    def test_symlink_target_mtime_used(self):
        # Simulate omarchy's symlink shape: the real colors.toml lives
        # inside a theme directory; `current/theme` is a symlink to it.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            themes_dir = home_path / ".config/omarchy/themes/tokyonight"
            themes_dir.mkdir(parents=True)
            (themes_dir / "colors.toml").write_text(_DARK_PALETTE)
            # Build the symlink chain that omarchy uses.
            current = home_path / ".config/omarchy/current"
            current.mkdir()
            (current / "theme").symlink_to(themes_dir)

            mtime = omarchy_theme_mtime(home=home_path)
        self.assertIsNotNone(mtime)


class TestOmarchyHelpers(unittest.TestCase):

    def test_omarchy_colors_path_defaults(self):
        with tempfile.TemporaryDirectory() as home:
            path = omarchy_colors_path(home=Path(home))
        self.assertTrue(str(path).endswith(".config/omarchy/current/theme/colors.toml"))

    def test_is_dark_recognizes_near_black(self):
        self.assertTrue(_is_dark("#000000"))
        self.assertTrue(_is_dark("#1a1b26"))

    def test_is_dark_recognizes_near_white(self):
        self.assertFalse(_is_dark("#ffffff"))
        self.assertFalse(_is_dark("#f5f5f5"))

    def test_shift_toward_foreground_moves_values(self):
        # 50% mix of pure black + pure white = mid-gray (7f or 80).
        out = _shift_toward_foreground("#000000", "#ffffff", 0.5)
        self.assertIn(out, ("#7f7f7f", "#808080"))


if __name__ == "__main__":
    unittest.main()
