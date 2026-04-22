"""Load the omarchy palette (if present) as a Textual theme.

Omarchy is a Linux desktop setup that manages a coordinated color palette
across terminal emulators, editors, and window managers. When a user
switches themes via `omarchy theme set <name>`, the symlink at
`~/.config/omarchy/current/theme` flips to point at the new theme
directory; its `colors.toml` holds the shared hex palette.

This module detects that file, builds a Textual `Theme`, and exposes a
way to re-read it so callers (the watch TUI) can poll for theme changes
and hot-swap.

Non-omarchy users never touch this — `load_omarchy_theme()` just returns
`None` when the file isn't there. Malformed input is also swallowed with
a warning so a broken palette never takes down the TUI.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Dict, Optional

from textual.theme import Theme


OMARCHY_THEME_NAME = "omarchy"


def omarchy_colors_path(home: Optional[Path] = None) -> Path:
    """Path to omarchy's active colors.toml. Does not check existence."""
    base = home if home is not None else Path.home()
    return base / ".config" / "omarchy" / "current" / "theme" / "colors.toml"


def omarchy_theme_mtime(home: Optional[Path] = None) -> Optional[float]:
    """Resolve the symlink chain and return the target's mtime.

    Returns None if the file doesn't exist or can't be statted. The
    resolve-before-stat matters: `omarchy theme set X` changes the
    symlink target, so statting the symlink itself may not reflect the
    change on all filesystems."""
    path = omarchy_colors_path(home)
    try:
        return path.resolve(strict=True).stat().st_mtime
    except (OSError, RuntimeError):
        return None


def load_omarchy_theme(home: Optional[Path] = None) -> Optional[Theme]:
    """Parse the active omarchy colors.toml into a Textual Theme.

    Returns None if the file isn't present or can't be parsed. A parse
    failure is reported on stderr so users can diagnose but never crashes
    the TUI."""
    path = omarchy_colors_path(home)
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        print(
            f"warning: could not read omarchy palette at {path}: {e}",
            file=sys.stderr,
        )
        return None

    try:
        return _build_theme(data)
    except (KeyError, ValueError, TypeError) as e:
        print(
            f"warning: omarchy palette at {path} is malformed: {e}",
            file=sys.stderr,
        )
        return None


def _build_theme(data: Dict[str, object]) -> Theme:
    """Map the omarchy palette onto Textual's theme slots.

    Missing keys fall back to values that render safely on most
    backgrounds. Callers should prefer a complete palette — the
    fallbacks exist so a partial/experimental colors.toml doesn't fail
    outright."""
    background = _hex(data, "background", "#1a1b26")
    foreground = _hex(data, "foreground", "#a9b1d6")
    accent = _hex(data, "accent", "#7aa2f7")

    # 16-slot ANSI palette — omarchy always provides these but the
    # fallbacks keep parsing robust for any partial config.
    red = _hex(data, "color1", "#f7768e")
    green = _hex(data, "color2", "#9ece6a")
    yellow = _hex(data, "color3", "#e0af68")
    magenta = _hex(data, "color5", "#ad8ee6")
    black = _hex(data, "color0", "#32344a")

    dark = _is_dark(background)
    return Theme(
        name=OMARCHY_THEME_NAME,
        primary=accent,
        secondary=magenta,
        accent=accent,
        warning=yellow,
        error=red,
        success=green,
        foreground=foreground,
        background=background,
        surface=_shift_toward_foreground(background, foreground, 0.08),
        panel=black,
        dark=dark,
    )


def _hex(data: Dict[str, object], key: str, fallback: str) -> str:
    """Read a hex string from the palette; normalize + validate."""
    raw = data.get(key, fallback)
    if not isinstance(raw, str):
        raise TypeError(f"{key} must be a string, got {type(raw).__name__}")
    value = raw.strip()
    if not value.startswith("#"):
        raise ValueError(f"{key}={value!r} must be a hex color starting with '#'")
    body = value[1:]
    if len(body) not in (3, 6):
        raise ValueError(f"{key}={value!r} must be 3 or 6 hex digits after '#'")
    try:
        int(body, 16)
    except ValueError as e:
        raise ValueError(f"{key}={value!r} is not valid hex: {e}")
    if len(body) == 3:
        return "#" + "".join(ch * 2 for ch in body.lower())
    return "#" + body.lower()


def _is_dark(hex_color: str) -> bool:
    """Relative-luminance dark/light test. Uses the standard formula
    from WCAG: < 0.5 is considered 'dark'."""
    r, g, b = _to_rgb(hex_color)
    # sRGB luminance approximation — simple linear weighting is enough
    # for a dark/light bucket decision.
    luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return luma < 0.5


def _shift_toward_foreground(bg: str, fg: str, amount: float) -> str:
    """Return bg shifted `amount` of the way toward fg.

    Used to derive a `surface` color that reads as a subtle lift above
    the base background — matches what most themes produce by hand."""
    br, bg_g, bb = _to_rgb(bg)
    fr, fg_g, fb = _to_rgb(fg)
    r = round(br + (fr - br) * amount)
    g = round(bg_g + (fg_g - bg_g) * amount)
    b = round(bb + (fb - bb) * amount)
    return "#{:02x}{:02x}{:02x}".format(
        max(0, min(255, r)),
        max(0, min(255, g)),
        max(0, min(255, b)),
    )


def _to_rgb(hex_color: str) -> tuple[int, int, int]:
    body = hex_color.lstrip("#")
    return (
        int(body[0:2], 16),
        int(body[2:4], 16),
        int(body[4:6], 16),
    )
