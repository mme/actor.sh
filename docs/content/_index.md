---
title: "actor.sh"
description: "A main actor session backed by parallel coding sub-agents in isolated git worktrees."
weight: 0
---

actor.sh manages multiple Claude Code or Codex agents running in parallel, each on its own branch in its own git worktree. You talk to one orchestrator session — `actor main` — and it spawns specialized sub-actors to handle the actual work, verifies their output, and reports back.

Start with [Getting started](getting-started/) for installation and your first actor. The [Concepts](concepts/) section explains the model — what an actor is, how roles, hooks, and channel notifications fit together. [Guides](guides/) cover task-shaped configuration topics, and [Reference](reference/) is a flat lookup of every CLI subcommand, MCP tool, and config key.
