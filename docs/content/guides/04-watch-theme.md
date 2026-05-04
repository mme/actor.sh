---
title: "Theming the Watch Dashboard"
description: "Built-in claude-dark / claude-light themes plus omarchy desktop integration."
weight: 4
slug: "watch-theme"
---

`actor watch` ships with two built-in themes — `claude-dark` and `claude-light` — and an opt-in omarchy integration that reflavors whichever one is active so the TUI matches your desktop palette. This guide covers both, plus the live-reload hook that keeps the dashboard in sync when you change desktop themes.

## Built-in themes

Out of the box you get `claude-dark` (the default) and `claude-light`. Both define the full slot palette the TUI uses — foreground, background, surface and panel chrome, primary and accent for focus rings, secondary for branding, and the semantic warning / error / success slots.

If you don't run omarchy, that's the whole story: the dashboard uses the base theme as defined.

## Omarchy flavoring

If actor.sh detects omarchy on your machine — specifically, the file `~/.config/omarchy/current/theme/colors.toml` — the active built-in theme is **flavored** at startup with the desktop's palette so the TUI reads as part of your environment rather than a foreign island.

The flavoring rules are:

- `foreground` ← omarchy `colors.toml`'s `foreground`
- `background` ← omarchy `colors.toml`'s `background`
- `surface`, `panel` ← `background` lifted ~8% toward `foreground` so panels stay subtly distinct from the desktop background while still being palette-derived
- `secondary` ← omarchy `colors.toml`'s `accent` (the brand / logo slot)
- `primary`, `accent` ← `hyprland.conf`'s `$activeBorderColor`, so focus rings match your active-window border

Semantic slots — `warning`, `error`, `success` — stay as the base theme defines them. `colors.toml` carries no semantic meaning, so there's nothing reasonable to override them with.

The flavor logic lives in `src/actor/watch/omarchy_theme.py`.

### Malformed input

If `colors.toml` can't be parsed, actor.sh logs a warning and keeps whatever theme is currently active rather than crashing the TUI. The same applies to `hyprland.conf` — the dashboard always renders.

## Live reload

Every 3 seconds, `actor watch` re-stats the resolved-target mtime of `colors.toml`. If it has changed — for example, because you ran `omarchy theme set <name>` — the flavor is rebuilt and re-registered under the same theme name. You don't need to restart the dashboard.

## Instant updates with the omarchy hook

3-second polling is fine for most use, but for instant theme switches actor.sh can hook into omarchy's own theme-set event:

```bash
actor setup --for omarchy
```

This installs a one-line fragment into `~/.config/omarchy/hooks/theme-set` that sends `SIGUSR2` to any running `actor watch` process. The watch process has the SIGUSR2 handler wired unconditionally — installation only adds the hook that sends the signal. As soon as you switch desktop themes, your dashboard reflavors immediately.

To remove the hook fragment:

```bash
actor setup --for omarchy --uninstall
```

The `--scope` and `--name` flags accepted by `actor setup` are ignored when `--for omarchy` is used; they're specific to the Claude Code MCP integration.

## Picking a base theme

The flavoring system overlays an omarchy palette on top of an existing theme — it does not pick the base theme for you. Choose `claude-dark` or `claude-light` from the dashboard's theme picker the same way you would on a non-omarchy machine, and the flavoring will follow.
