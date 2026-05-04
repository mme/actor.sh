---
title: "Installation"
description: "Get actor.sh installed and registered with Claude Code."
weight: 2
slug: "installation"
---

actor.sh ships as a Python package that exposes the `actor` CLI and an MCP server. The recommended path is to install with `uv`, register the bundled skill with Claude Code, and verify that the orchestrator session can see the `mcp__actor__*` tools.

## Prerequisites

You need two things on your `PATH` before starting:

- **`uv`** — Astral's Python package manager. It's the cleanest way to install Python CLI tools system-wide. If you don't have it yet:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- **`claude`** — the Claude Code CLI. actor.sh's orchestrator session is a Claude Code session with a system prompt and channel flag layered on top, so the binary needs to be installed and authenticated first. See the [Claude Code install docs](https://claude.com/claude-code) if you don't have it.

You can use `pip install actor-sh` instead of `uv` if that's already your workflow, but the rest of this guide assumes `uv`.

## Install the actor package

```bash
uv tool install actor-sh
```

This installs the `actor` command globally (in `~/.local/bin` by default). Verify it landed:

```bash
actor --version
```

You should see something like `actor-sh 0.1.x`. If the command isn't found, make sure `~/.local/bin` is on your `PATH`.

## Register with Claude Code

Installing the package isn't enough on its own — Claude Code needs to know about the bundled skill and the MCP server. One command wires both up:

```bash
actor setup --for claude-code
```

This is **idempotent**, so re-running it is safe.

"Registered" means two things happened:

1. The bundled `_skill/` directory (the skill that teaches Claude how to drive actor.sh) is deployed to Claude Code's skill directory.
2. The MCP server is added to Claude Code's MCP config under the name `actor`, so any new Claude Code session — and `actor main` in particular — sees the `mcp__actor__*` tools in its tool list.

### Scope: user vs project

By default `actor setup` installs **user-wide** (the skill and MCP are available in every Claude Code session you start). If you want the integration scoped to a single repository instead, pass `--scope project`:

```bash
actor setup --for claude-code --scope project
```

Project-scoped installs write into the current repo's local Claude Code config and only activate when you launch Claude Code from inside that repo.

### Renaming the registration

If you already have an `actor` MCP registered (for example, a development build alongside the published one), use `--name` to register under a different identifier:

```bash
actor setup --for claude-code --name actor-dev
```

The skill itself still calls the tools `mcp__<name>__*`, so a different name means the deployed skill references that name throughout.

## Verify the install

The simplest verification is to launch the orchestrator and check that the actor tools show up:

```bash
actor main
```

Inside the session, ask "what actor tools do you have available?" — Claude Code should list `mcp__actor__list_actors`, `mcp__actor__new_actor`, `mcp__actor__run_actor`, and the rest. If those tools are missing, the MCP server isn't connected; re-run `actor setup --for claude-code` and start a fresh Claude Code session.

## Updating after upgrades

When you bump `actor-sh` to a new version, the package upgrade alone doesn't refresh the deployed skill files — those were copied at setup time. Run:

```bash
actor update
```

`actor update` rewrites the deployed skill files in place so they match the version of `actor-sh` you just installed. Use `--scope project` if your install was project-scoped, and `--name <id>` if you registered under a non-default name. There's a built-in version check inside the skill that warns when the deployed copy drifts from the installed package, so you'll usually notice when it's time to run this.

You're now ready to launch your first actor — see [your first actor](../first-actor/).
