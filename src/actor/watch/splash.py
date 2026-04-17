"""Full-screen splash with QHO animation shown when no actors exist."""

from __future__ import annotations

import math
import time
from bisect import bisect_right

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget


# -- ASCII logo ------------------------------------------------------------

LOGO = [
    r" █████╗  ██████╗████████╗ ██████╗ ██████╗    ███████╗██╗  ██╗",
    r"██╔══██╗██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗   ██╔════╝██║  ██║",
    r"███████║██║        ██║   ██║   ██║██████╔╝   ███████╗███████║",
    r"██╔══██║██║        ██║   ██║   ██║██╔══██╗   ╚════██║██╔══██║",
    r"██║  ██║╚██████╗   ██║   ╚██████╔╝██║  ██║██╗███████║██║  ██║",
    r"╚═╝  ╚═╝ ╚═════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═╝",
]
LOGO_H = len(LOGO)

TAGLINE = "Autonomous actor command center."
HINT = "Spawn one from Claude Code and it will appear here."


# -- QHO physics -----------------------------------------------------------
#
# 2D Quantum Harmonic Oscillator: ψ_{nx,ny}(x,y) = ψ_nx(x) · ψ_ny(y)
# with ψ_n(x) = e^{−x²/2} · H_n(x) / sqrt(2^n · n! · √π)
# Animate by crossfading amplitudes between 8 (nx, ny) pairs.

TEXTURE = " .,:;-~=+*x!tiLC0O8%#@"
N_TEX = len(TEXTURE)

STATES: list[tuple[int, int]] = [
    (0, 0), (1, 3), (2, 4), (4, 2), (3, 5), (6, 1), (4, 4), (2, 7),
]

CYCLE_SECONDS = 7.5
SPATIAL_RANGE = 3.0

# Threshold of amp² at which each bucket 0..N_TEX-1 becomes active.
# Derived from: intensity = (amp² * 18)^0.65, bucket = int(intensity * (N_TEX-1))
# → amp² threshold for bucket k is (k / (N_TEX-1))^(1/0.65) / 18
_INV_EXP = 1.0 / 0.65
_AMP_SQ_THRESHOLDS = [((k / (N_TEX - 1)) ** _INV_EXP) / 18.0 for k in range(N_TEX)]
_BOLD_FROM = int(0.75 * (N_TEX - 1)) + 1  # buckets 16..21 are bold


def _factorial(n: int) -> float:
    f = 1.0
    for i in range(2, n + 1):
        f *= i
    return f


def _hermite(n: int, x: float) -> float:
    if n == 0:
        return 1.0
    if n == 1:
        return 2.0 * x
    hm2 = 1.0
    hm1 = 2.0 * x
    for k in range(2, n + 1):
        h = 2.0 * x * hm1 - 2.0 * (k - 1) * hm2
        hm2, hm1 = hm1, h
    return hm1


def _psi1d(n: int, x: float) -> float:
    norm = 1.0 / math.sqrt((2 ** n) * _factorial(n) * math.sqrt(math.pi))
    return norm * math.exp(-0.5 * x * x) * _hermite(n, x)


def _psi2d(nx: int, ny: int, x: float, y: float) -> float:
    return _psi1d(nx, x) * _psi1d(ny, y)


# -- Widget ----------------------------------------------------------------


class Splash(Widget):
    """Full-screen QHO animation with ACTOR.SH logo overlay."""

    DEFAULT_CSS = """
    Splash {
        width: 1fr;
        height: 1fr;
        background: ansi_default;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._start = time.monotonic()
        self._cached_size: tuple[int, int] | None = None
        self._grids: list[list[list[float]]] | None = None
        self._style_cache: dict[str, Style] = {}

    def on_mount(self) -> None:
        self.set_interval(1 / 20, self.refresh)

    def _get_style(self, spec: str) -> Style:
        cached = self._style_cache.get(spec)
        if cached is None:
            cached = Style.parse(spec)
            self._style_cache[spec] = cached
        return cached

    def _ensure_grids(self, rows: int, cols: int) -> None:
        if self._cached_size == (rows, cols):
            return
        aspect = cols / max(1, rows) * 0.5  # terminal cells ~2x taller than wide
        grids: list[list[list[float]]] = []
        for (nx, ny) in STATES:
            grid = [[0.0] * cols for _ in range(rows)]
            for r in range(rows):
                y_norm = ((r + 0.5) / rows) * 2 - 1
                y = y_norm * SPATIAL_RANGE / aspect
                for c in range(cols):
                    x_norm = ((c + 0.5) / cols) * 2 - 1
                    x = x_norm * SPATIAL_RANGE
                    grid[r][c] = _psi2d(nx, ny, x, y)
            grids.append(grid)
        self._grids = grids
        self._cached_size = (rows, cols)

    def _theme_colors(self) -> tuple[str, str, str, str]:
        """(logo, anim, border, box_bg)."""
        t = self.app.current_theme
        if t is not None:
            return t.secondary, t.primary, t.foreground, t.panel
        return "#D77757", "#B1B9F9", "#FFFFFF", "#2C323E"

    def _compute_frame_params(self) -> tuple[float, float, int, int] | None:
        """Return (s, 1-s, idx, nxt) for the current time, or None if size is zero."""
        rows = self.size.height
        cols = self.size.width
        if rows <= 0 or cols <= 0:
            return None
        self._ensure_grids(rows, cols)

        t = time.monotonic() - self._start
        idx = int(t / CYCLE_SECONDS) % len(STATES)
        nxt = (idx + 1) % len(STATES)
        local = (t / CYCLE_SECONDS) - math.floor(t / CYCLE_SECONDS)
        s = 0.5 - 0.5 * math.cos(local * math.pi)
        return s, 1.0 - s, idx, nxt

    def render_line(self, y: int) -> Strip:
        params = self._compute_frame_params()
        if params is None or self._grids is None:
            return Strip.blank(self.size.width)
        s, one_minus_s, idx, nxt = params

        rows = self.size.height
        cols = self.size.width
        a_row = self._grids[idx][y]
        b_row = self._grids[nxt][y]

        logo_color, anim_color, border_color, panel_color = self._theme_colors()
        anim_plain_style = self._get_style(anim_color)
        anim_bold_style = self._get_style(f"bold {anim_color}")
        box_bg_style = self._get_style(f"on {panel_color}")
        border_style = self._get_style(f"{border_color} on {panel_color}")
        logo_style = self._get_style(f"bold {logo_color} on {panel_color}")
        tagline_style = self._get_style(f"{logo_color} on {panel_color}")
        hint_style = self._get_style(f"dim {anim_color} on {panel_color}")

        # Box geometry: border + padding + content
        overlay_lines = LOGO + ["", TAGLINE, "", HINT]
        overlay_h = len(overlay_lines)
        overlay_w = max(len(line) for line in overlay_lines)
        pad_v, pad_h = 1, 2
        box_w = min(cols, overlay_w + 2 * pad_h + 2)
        box_h = min(rows, overlay_h + 2 * pad_v + 2)
        box_r0 = max(0, (rows - box_h) // 2)
        box_c0 = max(0, (cols - box_w) // 2)

        br = y - box_r0
        in_box = 0 <= br < box_h

        # Build the per-column content for columns inside the box
        box_chars: list[str] = []
        box_styles: list[Style] = []
        if in_box:
            if br == 0:
                box_chars = ["╭"] + ["─"] * (box_w - 2) + ["╮"]
                box_styles = [border_style] * box_w
            elif br == box_h - 1:
                box_chars = ["╰"] + ["─"] * (box_w - 2) + ["╯"]
                box_styles = [border_style] * box_w
            else:
                box_chars = ["│"] + [" "] * (box_w - 2) + ["│"]
                box_styles = [border_style] + [box_bg_style] * (box_w - 2) + [border_style]
                content_row = br - 1 - pad_v
                if 0 <= content_row < overlay_h:
                    line = overlay_lines[content_row]
                    if line:
                        if content_row < LOGO_H:
                            line_style = logo_style
                        elif line == HINT:
                            line_style = hint_style
                        else:
                            line_style = tagline_style
                        # Center the content line within the inner box area
                        inner_start = 1 + pad_h
                        offset = inner_start + (overlay_w - len(line)) // 2
                        for i, ch in enumerate(line):
                            pos = offset + i
                            if 0 <= pos < box_w - 1 and ch != " ":
                                box_chars[pos] = ch
                                box_styles[pos] = line_style

        texture = TEXTURE
        thresholds = _AMP_SQ_THRESHOLDS

        segments: list[Segment] = []
        run_chars: list[str] = []
        run_style: Style | None = None

        def flush() -> None:
            if run_chars:
                segments.append(Segment("".join(run_chars), run_style))

        box_c1 = box_c0 + box_w
        for c in range(cols):
            if in_box and box_c0 <= c < box_c1:
                bc = c - box_c0
                ch = box_chars[bc]
                style = box_styles[bc]
            else:
                amp = one_minus_s * a_row[c] + s * b_row[c]
                amp_sq = amp * amp
                bucket = bisect_right(thresholds, amp_sq) - 1
                if bucket < 0:
                    bucket = 0
                elif bucket >= N_TEX:
                    bucket = N_TEX - 1
                ch = texture[bucket]
                style = anim_bold_style if bucket >= _BOLD_FROM else anim_plain_style

            if style is not run_style:
                flush()
                run_chars = []
                run_style = style
            run_chars.append(ch)

        flush()
        return Strip(segments, cols)

    def on_resize(self) -> None:
        self._cached_size = None
