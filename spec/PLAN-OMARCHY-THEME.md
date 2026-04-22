# Omarchy Theme Integration

## Goal

When the user is running omarchy, the `actor watch` TUI uses the omarchy
palette automatically and stays in sync when the omarchy theme changes.
Everywhere else, behavior is unchanged.

## Detection

Presence of `~/.config/omarchy/current/theme/colors.toml`. That file is a
small TOML palette:

```toml
accent = "#7aa2f7"
foreground = "#a9b1d6"
background = "#1a1b26"
color0 = "#32344a"
...
color15 = "#acb0d0"
```

`current/theme` is a symlink that `omarchy theme set <name>` flips to point
at a different theme directory — so detecting a theme change is equivalent
to detecting that the resolved target of `colors.toml` changed.

## Palette → Textual Theme mapping

| omarchy key | Textual slot |
|---|---|
| `background` | `background` |
| `foreground` | `foreground` |
| `accent` | `primary`, `accent` |
| `color1` | `error` |
| `color2` | `success` |
| `color3` | `warning` |
| `color5` | `secondary` |
| `color0` | `panel` |
| derived (`background` + 8% white) | `surface` |
| derived from `background` luminance | `dark` (bool) |

Missing keys → fall back to CLAUDE_DARK's value for that slot. Luminance
uses the standard relative-luminance formula; < 0.5 → dark.

## Load + live-reload behavior

- **At startup** (`on_ready`): if `colors.toml` is present, build the
  `OMARCHY` theme, register it, set it active. Otherwise use `CLAUDE_DARK`.
  Remember the resolved-path mtime so we can detect changes later.
- **Every 3 seconds** (via `set_interval`): re-stat the resolved path; if
  the mtime changed, rebuild the theme, re-register, re-activate. Theme
  name stays `"omarchy"` so Textual's re-registration handles the swap
  cleanly.
- **Non-TTY paths** (`actor watch --serve` for textual-serve browser
  output): the palette we pick at startup is fine; skip the live-reload
  interval since the server's clients wouldn't see mid-session changes
  anyway. Worth calling out in docs but not worth complicating the code.

## Error handling

- Missing file → return `None`, caller falls back to CLAUDE_DARK.
- Malformed TOML (parse error) / unreadable / broken symlink → log a
  one-line warning via `stderr`, return `None`, caller keeps whatever
  theme is active. Don't crash the TUI.
- Hex value malformed → same treatment as malformed TOML.

## Files

- **New:** `src/actor/watch/omarchy_theme.py` — `load_omarchy_theme()`,
  `omarchy_colors_path()`, `omarchy_theme_mtime()`, small luminance
  helper. ~100 LOC.
- **New:** `tests/test_omarchy_theme.py` — unit tests for the mapping,
  missing-file, malformed-TOML, light/dark detection. ~80 LOC.
- **Modified:** `src/actor/watch/app.py` — branch at theme registration;
  add live-reload interval.
- **Modified:** `src/actor/watch/themes.py` — no change needed if the new
  module is self-contained. Exports a module-level `OMARCHY_NAME =
  "omarchy"` constant if useful.
- **Modified:** `CLAUDE.md` — one paragraph in the watch / architecture
  section noting the auto-detect + live-reload behavior.

## Out of scope

- OSC 10/11 terminal-query fallback for non-omarchy users (separate
  future ticket if we want to extend coverage).
- Full ANSI-mode rendering (Textual `ansi_color=True`) — different
  tradeoff, discussed and rejected.
- Hooking into omarchy's `hooks/` mechanism for push-based theme
  changes (polling is fine for the cadence).
- Generating user CSS / terminal-theme files elsewhere in the system
  from actor-sh's config — this is read-only inbound integration.

## Non-goals worth naming

- Exposing an `actor` CLI flag to force a theme (`--theme omarchy` etc.)
  — users can live without, and it keeps the surface small.
- Parsing `colors.toml` fields we don't use (cursor, selection_*).
  Ignore them; forward-compat is free.
