"""Full-screen splash with QHO animation shown when no actors exist."""

from __future__ import annotations

import math
import time
from bisect import bisect_right

from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget


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


# 2D Quantum Harmonic Oscillator: ψ_{nx,ny}(x,y) = ψ_nx(x) · ψ_ny(y)
# with ψ_n(x) = e^{−x²/2} · H_n(x) / sqrt(2^n · n! · √π)
# Animated by crossfading amplitudes between 8 (nx, ny) pairs.

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


class Splash(Widget):
    """Full-screen QHO animation with ACTOR.SH logo overlay."""

    DEFAULT_CSS = """
    Splash {
        width: 1fr;
        height: 1fr;
        background: ansi_default;
    }
    """

    def __init__(self, animate: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self._animate = animate
        self._start = time.monotonic()
        self._cached_size: tuple[int, int] | None = None
        self._grids: list[list[list[float]]] | None = None
        self._style_cache: dict[str, Style] = {}
        # Three caches with different invalidation axes: _frame per tick,
        # _styles per theme change, _geometry per resize.
        self._frame_dirty = True
        self._frame: dict | None = None
        self._theme_key: str | None = None
        self._styles: dict[str, Style] | None = None
        self._geometry: dict | None = None
        self._geometry_size: tuple[int, int] | None = None

    def on_mount(self) -> None:
        if self._animate:
            self.set_interval(1 / 15, self._tick)

    def _tick(self) -> None:
        self._frame_dirty = True
        self.refresh()

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

        # ψ_{nx,ny}(x,y) factorizes as ψ_nx(x)·ψ_ny(y), so compute the 1D
        # values per row and per col once, then assemble each state's 2D
        # grid via cheap multiplies (35x faster than calling _psi2d per cell).
        x_coords = [(((c + 0.5) / cols) * 2 - 1) * SPATIAL_RANGE for c in range(cols)]
        y_coords = [(((r + 0.5) / rows) * 2 - 1) * SPATIAL_RANGE / aspect for r in range(rows)]
        unique_nx = {nx for nx, _ in STATES}
        unique_ny = {ny for _, ny in STATES}
        psi_x = {n: [_psi1d(n, x) for x in x_coords] for n in unique_nx}
        psi_y = {n: [_psi1d(n, y) for y in y_coords] for n in unique_ny}

        grids: list[list[list[float]]] = []
        for (nx, ny) in STATES:
            px = psi_x[nx]
            py = psi_y[ny]
            grids.append([[py_r * px_c for px_c in px] for py_r in py])
        self._grids = grids
        self._cached_size = (rows, cols)

    def _theme_colors(self) -> tuple[str, str, str, str]:
        """(logo, anim, border, box_bg)."""
        t = self.app.current_theme
        if t is not None:
            return t.secondary, t.primary, t.foreground, t.panel
        return "#D77757", "#B1B9F9", "#FFFFFF", "#2C323E"

    def _ensure_styles(self) -> None:
        t = self.app.current_theme
        key = t.name if t is not None else "_default"
        if self._styles is not None and self._theme_key == key:
            return
        logo_color, anim_color, border_color, panel_color = self._theme_colors()
        self._styles = {
            "anim": self._get_style(anim_color),
            "border": self._get_style(f"{border_color} on {panel_color}"),
            "box_bg": self._get_style(f"on {panel_color}"),
            "logo": self._get_style(f"bold {logo_color} on {panel_color}"),
            "tagline": self._get_style(f"{logo_color} on {panel_color}"),
            "hint": self._get_style(f"dim {anim_color} on {panel_color}"),
        }
        self._theme_key = key
        # Box segments depend on styles — invalidate
        if self._geometry is not None:
            self._geometry["box_segments"] = None

    def _ensure_geometry(self, rows: int, cols: int) -> None:
        if self._geometry is not None and self._geometry_size == (rows, cols):
            return
        overlay_lines = LOGO + ["", TAGLINE, "", HINT]
        overlay_h = len(overlay_lines)
        overlay_w = max(len(line) for line in overlay_lines)
        pad_v, pad_h = 2, 3
        box_w = min(cols, overlay_w + 2 * pad_h + 2)
        box_h = min(rows, overlay_h + 2 * pad_v + 2)
        box_r0 = max(0, (rows - box_h) // 2)
        box_c0 = max(0, (cols - box_w) // 2)

        # Terminal too small for a box — skip the overlay entirely.
        # Min viable: 2 border cols + 1 interior + border = 4 wide, 2 tall.
        if box_w < 4 or box_h < 2:
            self._geometry = {
                "box_w": 0,
                "box_h": 0,
                "box_r0": 0,
                "box_c0": 0,
                "box_c1": 0,
                "rows_content": [],
                "box_segments": [],
            }
            self._geometry_size = (rows, cols)
            return

        rows_content: list[tuple[list[str], list[str]]] = []
        for br in range(box_h):
            if br == 0:
                chars = ["╭"] + ["─"] * (box_w - 2) + ["╮"]
                keys = ["border"] * box_w
            elif br == box_h - 1:
                chars = ["╰"] + ["─"] * (box_w - 2) + ["╯"]
                keys = ["border"] * box_w
            else:
                chars = ["│"] + [" "] * (box_w - 2) + ["│"]
                keys = ["border"] + ["box_bg"] * (box_w - 2) + ["border"]
                content_row = br - 1 - pad_v
                if 0 <= content_row < overlay_h:
                    line = overlay_lines[content_row]
                    if line:
                        if content_row < LOGO_H:
                            line_key = "logo"
                        elif line == HINT:
                            line_key = "hint"
                        else:
                            line_key = "tagline"
                        inner_start = 1 + pad_h
                        offset = inner_start + (overlay_w - len(line)) // 2
                        for i, ch in enumerate(line):
                            pos = offset + i
                            if 0 <= pos < box_w - 1 and ch != " ":
                                chars[pos] = ch
                                keys[pos] = line_key
            rows_content.append((chars, keys))

        self._geometry = {
            "box_w": box_w,
            "box_h": box_h,
            "box_r0": box_r0,
            "box_c0": box_c0,
            "box_c1": box_c0 + box_w,
            "rows_content": rows_content,
            "box_segments": None,  # built lazily once styles are available
        }
        self._geometry_size = (rows, cols)

    def _ensure_box_segments(self) -> None:
        assert self._geometry is not None and self._styles is not None
        if self._geometry["box_segments"] is not None:
            return
        styles = self._styles
        per_row: list[list[Segment]] = []
        for chars, keys in self._geometry["rows_content"]:
            segs: list[Segment] = []
            run_chars: list[str] = []
            run_key: str | None = None
            for i in range(len(chars)):
                k = keys[i]
                if k != run_key:
                    if run_chars:
                        segs.append(Segment("".join(run_chars), styles[run_key]))
                        run_chars = []
                    run_key = k
                run_chars.append(chars[i])
            if run_chars:
                segs.append(Segment("".join(run_chars), styles[run_key]))
            per_row.append(segs)
        self._geometry["box_segments"] = per_row

    def _prepare_frame(self) -> None:
        rows = self.size.height
        cols = self.size.width
        if rows <= 0 or cols <= 0:
            self._frame = None
            return
        self._ensure_grids(rows, cols)
        self._ensure_styles()
        self._ensure_geometry(rows, cols)
        self._ensure_box_segments()

        t = (time.monotonic() - self._start) if self._animate else 0.0
        idx = int(t / CYCLE_SECONDS) % len(STATES)
        nxt = (idx + 1) % len(STATES)
        local = (t / CYCLE_SECONDS) - math.floor(t / CYCLE_SECONDS)
        s = 0.5 - 0.5 * math.cos(local * math.pi)

        self._frame = {
            "s": s,
            "one_minus_s": 1.0 - s,
            "a_grid": self._grids[idx],
            "b_grid": self._grids[nxt],
            "rows": rows,
            "cols": cols,
        }

    def _anim_segment(
        self,
        start: int,
        end: int,
        a_row: list[float],
        b_row: list[float],
        s: float,
        one_minus_s: float,
        style: Style,
    ) -> Segment:
        thresholds = _AMP_SQ_THRESHOLDS
        texture = TEXTURE
        n_tex_m1 = N_TEX - 1
        chars: list[str] = []
        for c in range(start, end):
            amp = one_minus_s * a_row[c] + s * b_row[c]
            amp_sq = amp * amp
            bucket = bisect_right(thresholds, amp_sq) - 1
            if bucket < 0:
                bucket = 0
            elif bucket > n_tex_m1:
                bucket = n_tex_m1
            chars.append(texture[bucket])
        return Segment("".join(chars), style)

    def render_line(self, y: int) -> Strip:
        if self._frame_dirty:
            self._prepare_frame()
            self._frame_dirty = False

        frame = self._frame
        if frame is None:
            return Strip.blank(self.size.width)

        cols = frame["cols"]
        a_row = frame["a_grid"][y]
        b_row = frame["b_grid"][y]
        s = frame["s"]
        one_minus_s = frame["one_minus_s"]
        anim_style = self._styles["anim"]

        geom = self._geometry
        box_r0 = geom["box_r0"]
        box_h = geom["box_h"]
        box_c0 = geom["box_c0"]
        box_c1 = geom["box_c1"]
        br = y - box_r0
        in_box = 0 <= br < box_h

        if not in_box:
            return Strip(
                [self._anim_segment(0, cols, a_row, b_row, s, one_minus_s, anim_style)],
                cols,
            )

        segments: list[Segment] = []
        if box_c0 > 0:
            segments.append(self._anim_segment(0, box_c0, a_row, b_row, s, one_minus_s, anim_style))
        segments.extend(geom["box_segments"][br])
        if box_c1 < cols:
            segments.append(self._anim_segment(box_c1, cols, a_row, b_row, s, one_minus_s, anim_style))
        return Strip(segments, cols)

    def on_resize(self) -> None:
        self._cached_size = None
        self._geometry = None
        self._frame_dirty = True
