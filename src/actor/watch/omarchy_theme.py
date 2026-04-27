"""Blend the omarchy palette (if present) onto a base Textual theme.

Omarchy is a Linux desktop setup that manages a coordinated color palette
across terminal emulators, editors, and window managers. When a user
switches themes via `omarchy theme set <name>`, the symlink at
`~/.config/omarchy/current/theme` flips to point at the new theme
directory; its `colors.toml` holds the shared hex palette and
`hyprland.conf` declares the active-window-border highlight.

This module reads both and returns a *flavored* variant of a supplied
base theme. Today we pull two slots from omarchy:

- `foreground` from `colors.toml` — so body text matches the desktop's
  default FG, giving plain prose the "feels-native" look Claude Code
  gets for free from unstyled line output.
- `primary` (and `accent`) from `hyprland.conf`'s `$activeBorderColor`
  — so focus rings, selected rows, and active tabs glow the same color
  as the user's active-window border.

Every other slot (brand colors, semantic warn/err/success, backgrounds)
stays as the base theme defines it, so the TUI still reads as
"Actor.sh" rather than becoming a pure palette swap.

Non-omarchy users never touch this — `apply_omarchy_flavor()` returns
the base unchanged when the files aren't there. Malformed input is
swallowed with a warning so a broken palette never takes down the TUI.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Dict, Optional

from textual.theme import Theme


def omarchy_colors_path(home: Optional[Path] = None) -> Path:
    """Path to omarchy's active colors.toml. Does not check existence."""
    base = home if home is not None else Path.home()
    return base / ".config" / "omarchy" / "current" / "theme" / "colors.toml"


def omarchy_hyprland_path(home: Optional[Path] = None) -> Path:
    """Path to omarchy's active hyprland.conf. Does not check existence.

    We don't stat this for the live-reload mtime check — colors.toml
    and hyprland.conf live in the same theme directory, so a theme
    switch flips them together. One stat covers both files' freshness.
    """
    base = home if home is not None else Path.home()
    return base / ".config" / "omarchy" / "current" / "theme" / "hyprland.conf"


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
    or the palette file is unreadable / malformed."""
    data = _load_palette(home)
    if data is None:
        return None
    active_border = _load_active_border(home)
    try:
        return _flavor(base, data, active_border)
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


def _flavor(
    base: Theme,
    data: Dict[str, object],
    active_border: Optional[str],
) -> Theme:
    """Overlay omarchy values onto `base`. Keep the base's name so it
    shows up under its existing picker entry (e.g. 'claude-dark') — the
    user doesn't see a separate 'omarchy' item.

    Overridden slots:
    - `foreground` ← colors.toml's foreground (desktop-native body text)
    - `background` ← colors.toml's background
    - `surface` and `panel` ← background lifted toward foreground
      (subtle lift so widgets/panels still read as distinct from the
      desktop bg without introducing a non-palette color)
    - `secondary` ← whichever of `accent` / `color3` / `color5` /
      `color1` is most distant in RGB from the active-border color,
      so the brand/logo slot doesn't collapse into `primary` (e.g.
      tokyo night sets accent == active-border)
    - `primary` and `accent` ← hyprland.conf's $activeBorderColor
      (TUI focus rings match active-window border)

    Semantic slots (`warning`, `error`, `success`) stay as `base` had
    them — colors.toml doesn't carry semantic meaning per slot, and
    swapping them out arbitrarily from numbered colorN entries would
    risk red-as-success collisions."""
    foreground = _hex(data, "foreground", base.foreground)
    background = _hex(data, "background", base.background)
    surface = _shift_toward_foreground(background, foreground, 0.08)
    primary = active_border if active_border is not None else base.primary
    accent = active_border if active_border is not None else base.accent
    secondary = _pick_distinct(
        data,
        exclude=primary,
        candidates=("accent", "color3", "color5", "color1"),
        fallback=base.secondary,
    )
    return Theme(
        name=base.name,
        primary=primary,
        secondary=secondary,
        accent=accent,
        warning=base.warning,
        error=base.error,
        success=base.success,
        foreground=foreground,
        background=background,
        surface=surface,
        panel=surface,
        dark=base.dark,
    )


# Two palette colors are "distinguishable" if their RGB-space distance
# exceeds this threshold. ~85 in 0-255 RGB = a perceptible hue/value
# shift that's not just dithered noise. Tuned by eye against tokyo
# night (accent == active-border) where we want secondary to skip past
# `accent` to a real second color; against gruvbox where accent and
# color3 are both yellow-orange but visibly distinct.
_DISTINCT_THRESHOLD = 85.0


def _pick_distinct(
    data: Dict[str, object],
    exclude: Optional[str],
    candidates: tuple[str, ...],
    fallback: str,
) -> str:
    """Walk `candidates` in order; return the first hex value far
    enough from `exclude` to read as a different color. If `exclude`
    is None (no active-border file) the first valid candidate wins."""
    farthest: tuple[Optional[str], float] = (None, 0.0)
    for key in candidates:
        try:
            color = _hex(data, key, "")
        except (TypeError, ValueError):
            continue
        if not color:
            continue
        if exclude is None:
            return color
        dist = _rgb_distance(color, exclude)
        if dist >= _DISTINCT_THRESHOLD:
            return color
        if dist > farthest[1]:
            farthest = (color, dist)
    # Nothing met the threshold — return the most-distant candidate we
    # found (if any), else the base fallback. Better to drift toward
    # similar than to pull a totally off-palette color.
    return farthest[0] if farthest[0] is not None else fallback


def _rgb_distance(a: str, b: str) -> float:
    ar, ag, ab = _to_rgb(a)
    br, bg, bb = _to_rgb(b)
    return ((ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2) ** 0.5


# Matches `$activeBorderColor = rgb(7aa2f7)` or `rgba(7aa2f7, 1.0)` at
# the start of a line. Hyprland's config language accepts `$variable =
# value` assignments and rgb()/rgba() color literals with bare hex
# (no leading #). Alpha is parsed but ignored — Textual primary doesn't
# do per-slot alpha.
_ACTIVE_BORDER_RE = re.compile(
    r"^\s*\$activeBorderColor\s*=\s*rgba?\(\s*([0-9a-fA-F]{6})",
    re.MULTILINE,
)


def _load_active_border(home: Optional[Path]) -> Optional[str]:
    """Extract $activeBorderColor from omarchy's hyprland.conf as a
    '#rrggbb' string. Returns None if the file isn't there, can't be
    read, or doesn't declare the variable in the expected shape."""
    path = omarchy_hyprland_path(home)
    if not path.is_file():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        print(
            f"warning: could not read omarchy hyprland.conf at {path}: {e}",
            file=sys.stderr,
        )
        return None
    match = _ACTIVE_BORDER_RE.search(text)
    if match is None:
        return None
    return "#" + match.group(1).lower()


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
