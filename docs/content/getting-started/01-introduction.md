---
title: "Introduction"
description: "What actor.sh is and why it exists."
slug: "introduction"
weight: 1
---

actor.sh manages multiple Claude/Codex coding agents running in parallel, each in its own isolated git worktree. You talk to one main actor — the session you launch with `actor main` — and it spawns specialized peer actors to handle the actual work, verifies their output, and reports back.

## The problem

Running several coding agents in parallel sounds like leverage right up until you're the one routing it. Without a coordinator, you end up as the middle manager: opening terminals, branching, copy-pasting prompts and outputs between them, remembering which one is doing what, checking back on each, and stitching the results together yourself. The throughput goes up; the cognitive overhead goes up faster. There's no overview, no completion signal, and switching contexts costs more than the parallel work saved.

actor.sh moves the coordination layer into the conversation you're already having with your main coding agent.

## What's an actor

An actor is a coding agent — `claude` or `codex` — running a task on its own git branch, inside its own working tree under `~/.actor/worktrees/<actor-name>/`. The branch and worktree are created when the actor is spawned and stay around across runs, so the actor's edits never collide with your main checkout or with another actor's work.

Each actor keeps its own conversational context. Calling `run_actor` again on an existing actor resumes its session — it remembers what you discussed last time, what it tried, and what the user asked for. That's the difference between handing work off and starting over: you can come back to an actor an hour later or a week later and continue the same thread.

For the full mental model — lifecycle, parent/child tracking, what `discard` does and doesn't clean up — see [Actors and worktrees](../../concepts/actors-and-worktrees/).

## How parallel runs stay coordinated

Spawning an actor returns immediately; the agent runs in the background. When it finishes, the MCP server pushes a `notifications/claude/channel` event, which arrives in the main actor's session as a `<channel source="actor" ...>` block carrying the actor's output and final status. No polling, no watchdog loop — the main actor learns about completions as they happen and decides what to do next: relay a result to you, queue follow-up work, or fan out the next batch.

This is what lets the main actor act like a coordinator instead of a fire-and-forget launcher. See [Channel notifications](../../concepts/channel-notifications/) for the wire shape, and the agent-compatibility notes (Claude Code is fully supported; Codex doesn't yet forward MCP server notifications, so it works as an actor target but not as the orchestrator).

## Actors vs subagents

Actors and Claude Code's built-in subagents solve different problems and compose well together. A subagent is a short-lived helper one actor dispatches inside a single job — for parallel reads, scoped searches, or independent fixes that the actor will fold back together itself. Subagents finish, return, and are gone.

An actor is a peer collaborator: a separately running session you can return to, course-correct, hand the next milestone to, or ask for a revision on a previous result. Use a subagent when you want parallel throughput inside one task. Use an actor when you want a continuing thread — work you'll come back to, or work that should run on its own branch with its own context.

## What this site covers

The rest of the docs are organized by what you're trying to do:

- **[Getting started](../)** — install actor.sh, register the skill and MCP server with Claude Code ([Installation](../installation/)), then spawn your first actor end-to-end ([Your first actor](../first-actor/)).
- **[Concepts](../../concepts/)** — the model: actors and worktrees, roles, lifecycle hooks, ask blocks, channel notifications.
- **[Guides](../../guides/)** — task-shaped walkthroughs: configuring the Claude or Codex agent, the full `settings.kdl` tour, theming the watch dashboard.
- **[Reference](../../reference/)** — flat lookup tables for every CLI subcommand, every MCP tool, and every config key per agent.

If you're new, the recommended path is straight through Getting started, then into Concepts when you want to understand the model behind what you've already used.
