"""Omarchy palette → hybrid Textual theme."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from textual.theme import Theme

from actor.watch.omarchy_theme import (
    _hex,
    _is_dark,
    _load_active_border,
    _shift_toward_foreground,
    apply_omarchy_flavor,
    omarchy_colors_path,
    omarchy_hyprland_path,
    omarchy_theme_mtime,
)


# A minimal base theme used to exercise the flavor logic without pulling
# in CLAUDE_DARK's hex values (keeps assertions about what survived vs.
# what the omarchy palette overrode unambiguous).
_BASE = Theme(
    name="test-base",
    primary="#000001",
    secondary="#000002",
    accent="#000003",
    warning="#000004",
    error="#000005",
    success="#000006",
    foreground="#111111",
    background="#222222",
    surface="#333333",
    panel="#444444",
    dark=True,
)


def _write_palette(home: Path, body: str) -> Path:
    target = home / ".config" / "omarchy" / "current" / "theme" / "colors.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


def _write_hyprland(home: Path, body: str) -> Path:
    target = home / ".config" / "omarchy" / "current" / "theme" / "hyprland.conf"
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


class TestApplyOmarchyFlavor(unittest.TestCase):

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(apply_omarchy_flavor(_BASE, home=Path(home)))

    def test_palette_overrides_environmental_slots(self):
        # No hyprland.conf → palette-only overrides still apply (the
        # window-border file is optional).
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), _DARK_PALETTE)
            out = apply_omarchy_flavor(_BASE, home=Path(home))
        self.assertIsNotNone(out)
        assert out is not None
        # Palette-driven slots…
        self.assertEqual(out.foreground, "#a9b1d6")
        self.assertEqual(out.background, "#1a1b26")
        self.assertEqual(out.panel, out.surface)  # both lifted from bg
        # No active-border to dodge → first candidate (accent) wins.
        self.assertEqual(out.secondary, "#7aa2f7")
        # No active-border file → primary/accent stay on base.
        self.assertEqual(out.primary, "#000001")
        self.assertEqual(out.accent, "#000003")
        # Semantic slots untouched.
        self.assertEqual(out.warning, "#000004")
        self.assertEqual(out.error, "#000005")
        self.assertEqual(out.success, "#000006")
        self.assertEqual(out.dark, True)
        self.assertEqual(out.name, _BASE.name)

    def test_surface_is_lifted_from_background(self):
        # surface = bg shifted ~8% toward fg, so it sits between them
        # rather than mirroring either slot.
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), _DARK_PALETTE)
            out = apply_omarchy_flavor(_BASE, home=Path(home))
        assert out is not None
        self.assertNotEqual(out.surface, out.background)
        self.assertNotEqual(out.surface, out.foreground)

    def test_active_border_overrides_primary_and_accent(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), _DARK_PALETTE)
            _write_hyprland(
                Path(home),
                "$activeBorderColor = rgb(7aa2f7)\n\n"
                "general {\n    col.active_border = $activeBorderColor\n}\n",
            )
            out = apply_omarchy_flavor(_BASE, home=Path(home))
        assert out is not None
        self.assertEqual(out.primary, "#7aa2f7")
        self.assertEqual(out.accent, "#7aa2f7")
        # accent equals active-border in tokyo night, so secondary skips
        # it and lands on color3 — a perceptibly different palette slot.
        self.assertNotEqual(out.secondary, out.primary)
        self.assertEqual(out.secondary, "#e0af68")  # color3 (warm yellow)

    def test_light_palette_overrides_environmental_slots(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), _LIGHT_PALETTE)
            out = apply_omarchy_flavor(_BASE, home=Path(home))
        assert out is not None
        self.assertEqual(out.foreground, "#222222")
        self.assertEqual(out.background, "#f5f5f5")
        self.assertEqual(out.secondary, "#2e5aa0")
        # `dark` stays as base had it — we don't derive it from
        # background luminance, so the user's chosen base controls the
        # CSS class regardless of the palette.
        self.assertTrue(out.dark)

    def test_malformed_toml_returns_none_and_warns(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), "this = isn't valid TOML\nno no no")
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = apply_omarchy_flavor(_BASE, home=Path(home))
        self.assertIsNone(result)
        self.assertIn("warning", buf.getvalue().lower())

    def test_missing_foreground_key_falls_back_to_base(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), 'background = "#000000"\n')
            out = apply_omarchy_flavor(_BASE, home=Path(home))
        assert out is not None
        self.assertEqual(out.foreground, _BASE.foreground)

    def test_non_string_value_rejected_as_warning(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), "foreground = 42\n")
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = apply_omarchy_flavor(_BASE, home=Path(home))
        self.assertIsNone(result)
        self.assertIn("malformed", buf.getvalue().lower())

    def test_hex_without_hash_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            _write_palette(Path(home), 'foreground = "a9b1d6"\n')
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = apply_omarchy_flavor(_BASE, home=Path(home))
        self.assertIsNone(result)


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
        # Mirror omarchy's on-disk shape: `current/theme` is a symlink
        # to the active theme directory. Resolving must reach the target
        # so an `omarchy theme set X` flip (new symlink target) is
        # observable as a different mtime.
        with tempfile.TemporaryDirectory() as home:
            home_path = Path(home)
            themes_dir = home_path / ".config/omarchy/themes/tokyonight"
            themes_dir.mkdir(parents=True)
            (themes_dir / "colors.toml").write_text(_DARK_PALETTE)
            current = home_path / ".config/omarchy/current"
            current.mkdir()
            (current / "theme").symlink_to(themes_dir)

            mtime = omarchy_theme_mtime(home=home_path)
        self.assertIsNotNone(mtime)


class TestLoadActiveBorder(unittest.TestCase):

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(_load_active_border(Path(home)))

    def test_parses_rgb_literal(self):
        with tempfile.TemporaryDirectory() as home:
            _write_hyprland(Path(home), "$activeBorderColor = rgb(7aa2f7)\n")
            self.assertEqual(_load_active_border(Path(home)), "#7aa2f7")

    def test_parses_rgba_literal_ignoring_alpha(self):
        with tempfile.TemporaryDirectory() as home:
            _write_hyprland(Path(home), "$activeBorderColor = rgba(7aa2f7, 1.0)\n")
            self.assertEqual(_load_active_border(Path(home)), "#7aa2f7")

    def test_missing_variable_returns_none(self):
        with tempfile.TemporaryDirectory() as home:
            _write_hyprland(
                Path(home),
                "general {\n    col.active_border = rgb(123456)\n}\n",
            )
            self.assertIsNone(_load_active_border(Path(home)))

    def test_normalizes_case(self):
        with tempfile.TemporaryDirectory() as home:
            _write_hyprland(Path(home), "$activeBorderColor = rgb(7AA2F7)\n")
            self.assertEqual(_load_active_border(Path(home)), "#7aa2f7")


class TestOmarchyHelpers(unittest.TestCase):

    def test_omarchy_hyprland_path_defaults(self):
        with tempfile.TemporaryDirectory() as home:
            path = omarchy_hyprland_path(home=Path(home))
        self.assertTrue(str(path).endswith(".config/omarchy/current/theme/hyprland.conf"))

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
        out = _shift_toward_foreground("#000000", "#ffffff", 0.5)
        self.assertIn(out, ("#7f7f7f", "#808080"))

    def test_three_digit_hex_normalized_to_six(self):
        out = _hex({"k": "#abc"}, "k", "#000000")
        self.assertEqual(out, "#aabbcc")


if __name__ == "__main__":
    unittest.main()
