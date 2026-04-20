"""Key + mouse event -> byte sequence translator.

Reference: xterm control sequences at
https://invisible-island.net/xterm/ctlseqs/ctlseqs.html
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


# DECCKM (param 1) flips arrow/nav keys between CSI (\x1b[A …) and
# SS3 (\x1bOA …). The child advertises its preference via DECSET.
_CSI: Dict[str, str] = {
    "up":        "\x1b[A",
    "down":      "\x1b[B",
    "right":     "\x1b[C",
    "left":      "\x1b[D",
    "home":      "\x1b[H",
    "end":       "\x1b[F",
}

_SS3: Dict[str, str] = {
    "up":        "\x1bOA",
    "down":      "\x1bOB",
    "right":     "\x1bOC",
    "left":      "\x1bOD",
    "home":      "\x1bOH",
    "end":       "\x1bOF",
}

# Common fixed keys (no app-cursor variant).
_FIXED: Dict[str, str] = {
    "enter":      "\r",
    "tab":        "\t",
    "shift+tab":  "\x1b[Z",
    "escape":     "\x1b",
    "backspace":  "\x7f",
    "delete":     "\x1b[3~",
    "insert":     "\x1b[2~",
    "pageup":     "\x1b[5~",
    "pagedown":   "\x1b[6~",
    "f1":         "\x1bOP",
    "f2":         "\x1bOQ",
    "f3":         "\x1bOR",
    "f4":         "\x1bOS",
    "f5":         "\x1b[15~",
    "f6":         "\x1b[17~",
    "f7":         "\x1b[18~",
    "f8":         "\x1b[19~",
    "f9":         "\x1b[20~",
    "f10":        "\x1b[21~",
    "f11":        "\x1b[23~",
    "f12":        "\x1b[24~",
}


def key_to_bytes(
    key: str,
    character: Optional[str] = None,
    *,
    app_cursor: bool = False,
) -> Optional[bytes]:
    """Translate a key press to PTY bytes.

    `key` is a normalized name like "up", "ctrl+c", "tab". Modifier prefixes
    supported: "ctrl+", "alt+". `character` is the printable glyph if any
    (caller owns unicode normalization). `app_cursor` enables DECCKM SS3
    for arrow/home/end.

    Returns None for keys we don't want to forward (caller can ignore them).
    """
    # Fixed mappings first.
    if key in _FIXED:
        return _FIXED[key].encode()

    # Arrows / home / end — app_cursor switches prefix.
    table = _SS3 if app_cursor else _CSI
    if key in table:
        return table[key].encode()

    # Ctrl+<letter> and a few common ctrl combos.
    if key.startswith("ctrl+") and len(key) == 6:
        letter = key[5].lower()
        if "a" <= letter <= "z":
            return bytes([ord(letter) - ord("a") + 1])
        if letter == " " or letter == "@":
            return b"\x00"
        if letter == "[":
            return b"\x1b"
        if letter == "\\":
            return b"\x1c"
        if letter == "]":
            return b"\x1d"
        if letter == "^":
            return b"\x1e"
        if letter == "_":
            return b"\x1f"

    # Alt+<letter> = ESC <letter>.
    if key.startswith("alt+") and len(key) == 5:
        return b"\x1b" + key[4].encode()

    # Printable character — always forward.
    if character is not None:
        try:
            return character.encode("utf-8")
        except UnicodeEncodeError:
            return None

    return None


# --- Mouse ---------------------------------------------------------------

class MouseButton(Enum):
    """Button identity. Release is an event phase, not a button — see
    MouseEventKind. WHEEL_UP / WHEEL_DOWN use xterm's pseudo-button 64/65."""
    LEFT = 0
    MIDDLE = 1
    RIGHT = 2
    WHEEL_UP = 64
    WHEEL_DOWN = 65


class MouseEventKind(Enum):
    PRESS = "press"
    RELEASE = "release"


@dataclass(frozen=True)
class MouseMode:
    """DECSET mouse-reporting modes the child has enabled.
    tracking=1000, drag=1002, any_motion=1003, sgr=1006."""
    tracking: bool = False
    drag: bool = False
    any_motion: bool = False
    sgr: bool = False

    def should_report_click(self) -> bool:
        return self.tracking or self.drag or self.any_motion


def mouse_press_to_bytes(
    button: MouseButton,
    x: int, y: int,
    mode: MouseMode,
) -> Optional[bytes]:
    """Encode a mouse press or wheel event. 0-based cell coords in;
    1-based xterm-protocol coords out. None if tracking is off."""
    if not mode.should_report_click():
        # Wheel/scroll events require tracking to be on per xterm protocol.
        return None

    cb = button.value
    cx = x + 1
    cy = y + 1
    if mode.sgr:
        return f"\x1b[<{cb};{cx};{cy}M".encode()
    # Legacy X10: CSI M Cb Cx Cy with all values offset by 32.
    # Clamp to the legacy protocol's 223-cell limit.
    cb_b = 32 + cb
    cx_b = 32 + min(cx, 223)
    cy_b = 32 + min(cy, 223)
    return bytes([0x1b, ord("["), ord("M"), cb_b, cx_b, cy_b])


def mouse_release_to_bytes(
    x: int, y: int,
    mode: MouseMode,
    button: MouseButton = MouseButton.LEFT,
) -> Optional[bytes]:
    """SGR mode distinguishes the released button (lowercase m terminator).
    Legacy X10 emits a single "release" code (3) regardless of button."""
    if not mode.should_report_click():
        return None
    cx = x + 1
    cy = y + 1
    if mode.sgr:
        return f"\x1b[<{button.value};{cx};{cy}m".encode()
    return bytes([0x1b, ord("["), ord("M"), 32 + 3, 32 + min(cx, 223), 32 + min(cy, 223)])
