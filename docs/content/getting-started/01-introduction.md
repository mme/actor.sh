---
title: "Introduction"
description: "What actor.sh is and why it exists."
slug: "introduction"
weight: 1
---

actor.sh manages multiple Claude/Codex coding agents running in parallel,
each in its own isolated git worktree. You talk to one main actor — the
session you launch with `actor main` — and it spawns specialized peer
actors to handle the actual work, verifies their output, and reports
back.

Each actor runs in its own worktree on its own branch, so parallel work
doesn't collide. Completion notifications flow back to the main actor
via a channel, so it knows when work is done without polling.

This documentation covers installation, the conceptual model, and
reference material for the CLI and configuration.
