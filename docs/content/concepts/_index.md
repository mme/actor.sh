---
title: "Concepts"
description: "The mental model: actors, roles, hooks, ask blocks, and channel notifications."
weight: 2
---

These pages explain the moving parts of actor.sh — what each piece is, why it exists, and how the parts fit together. They're the layer above the [reference](../reference/) docs: less "what flag does what" and more "what is this thing, conceptually."

- [Actors and worktrees](01-actors-and-worktrees/) — what an actor is, the isolated-worktree model, lifecycle from `new` through `discard`, and parent/child tracking.
- [Roles](02-roles/) — named presets in `settings.kdl` that bundle an agent, a system prompt, and config defaults.
- [Lifecycle hooks](03-hooks/) — shell commands that fire around create, run, and discard events.
- [Ask blocks](04-ask-blocks/) — customize when the orchestrator asks the user a question before key MCP calls.
- [Channel notifications](05-channel-notifications/) — how completion events flow from sub-actors back to the orchestrator without polling.

The [Guides](../guides/) section turns these concepts into task-shaped recipes (configuring agents, writing settings.kdl, theming the watch TUI).
