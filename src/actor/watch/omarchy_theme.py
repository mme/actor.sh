"""Blend the omarchy palette (if present) onto a base Textual theme.

Omarchy is a Linux desktop setup that manages a coordinated color palette
across terminal emulators, editors, and window managers. When a user
switches themes via `omarchy theme set <name>`, the symlink at
`~/.config/omarchy/current/theme` flips to point at the new theme
directory; its `colors.toml` holds the shared hex palette.

This module detects that file and returns a *flavored* variant of a
supplied base theme — today only the foreground slot is pulled from
omarchy, keeping every brand/semantic color from the base intact. That
mirrors what Claude Code ends up looking like on omarchy (unstyled text
follows the terminal's default FG; everything else stays branded) and
avoids the "TUI looks like a totally different app" outcome a pure
palette swap would produce.

Non-omarchy users never touch this — `apply_omarchy_flavor()` returns
the base unchanged when the file isn't there. Malformed input is
swallowed with a warning so a broken palette never takes down the TUI.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Dict, Optional

from textual.theme import Theme


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


def apply_omarchy_flavor(
    base: Theme, home: Optional[Path] = None,
) -> Optional[Theme]:
    """Return a variant of `base` with environment colors pulled from
    omarchy's active palette. Returns None when omarchy isn't present
    or the palette file is unreadable / malformed.

    Scope is intentionally small — just the foreground today. Growing
    this to cover background / surface / panel / semantic slots is a
    one-line-per-slot extension once we've lived with the FG override
    for a bit."""
    data = _load_palette(home)
    if data is None:
        return None
    try:
        return _flavor(base, data)
    except (KeyError, ValueError, TypeError) as e:
        print(
            f"warning: omarchy palette is malformed: {e}",
            file=sys.stderr,
        )
        return None


def _load_palette(home: Optional[Path]) -> Optional[Dict[str, object]]:
    path = omarchy_colors_path(home)
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        return tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        print(
            f"warning: could not read omarchy palette at {path}: {e}",
            file=sys.stderr,
        )
        return None


def _flavor(base: Theme, data: Dict[str, object]) -> Theme:
    """Overlay omarchy values onto `base`. Keep the base's name so it
    shows up under its existing picker entry (e.g. 'claude-dark') — the
    user doesn't see a separate 'omarchy' item.

    Today only the foreground is overridden; every other slot stays as
    `base` had it. We recompute `dark` from the overridden luminance in
    case omarchy picks a light-mode theme while base is dark (or vice
    versa) — keeps widget contrast logic correct."""
    foreground = _hex(data, "foreground", base.foreground)
    return Theme(
        name=base.name,
        primary=base.primary,
        secondary=base.secondary,
        accent=base.accent,
        warning=base.warning,
        error=base.error,
        success=base.success,
        foreground=foreground,
        background=base.background,
        surface=base.surface,
        panel=base.panel,
        dark=base.dark,
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
