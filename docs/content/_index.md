---
title: "actor.sh"
description: "What actor.sh is and why it exists."
weight: 0
aliases:
  - /getting-started/introduction/
  - /getting-started/
---

actor.sh manages multiple Claude/Codex coding agents running in parallel, each in its own isolated git worktree. You talk to one main actor — the session you launch with `actor main` — and it spawns specialized peer actors to handle the actual work, verifies their output, and reports back.

## The problem

Running several coding agents in parallel sounds like leverage right up until you're the one routing it. Without a coordinator, you end up as the middle manager: opening terminals, branching, copy-pasting prompts and outputs between them, remembering which one is doing what, checking back on each, and stitching the results together yourself. The throughput goes up; the cognitive overhead goes up faster. There's no overview, no completion signal, and switching contexts costs more than the parallel work saved.

actor.sh moves the coordination layer into the conversation you're already having with your main coding agent.

## What's an actor

An actor is a coding agent — `claude` or `codex` — running a task on its own git branch, inside its own working tree under `~/.actor/worktrees/<actor-name>/`. The branch and worktree are created when the actor is spawned and stay around across runs, so the actor's edits never collide with your main checkout or with another actor's work.

Each actor keeps its own conversational context. Calling `run_actor` again on an existing actor resumes its session — it remembers what you discussed last time, what it tried, and what the user asked for. That's the difference between handing work off and starting over: you can come back to an actor an hour later or a week later and continue the same thread.

See [Actors and worktrees](concepts/actors-and-worktrees/) for the full model.

## How parallel runs stay coordinated

Each actor's run executes in the background. When it finishes, the actor MCP server pushes a `<channel source="actor" ...>` event to the main actor's session — a structured notification carrying the actor's name, final status, and output. The main actor sees the event mid-conversation and reacts: verify the work, summarize, launch a follow-up, or report back to you.

That channel is what turns parallel runs from a cluster of orphaned terminals into one coherent thread of work. See [Channel notifications](concepts/channel-notifications/) for the event shape.

## Actors vs subagents

Subagents and actors look superficially similar — both are background helpers an agent dispatches — but they solve different problems.

A subagent is a short-lived helper a single actor spins up for parallel throughput inside one job: drafting four documentation pages at once, fanning out four search queries, fact-checking three claims in parallel. It runs its task and dissolves; its output flows back into the parent actor's reasoning.

An actor is a peer-level collaborator with its own working tree, its own context, and its own conversational thread. You can come back to it later and pick up where you left off, hand it the next milestone, ask for a revision. Actors are how distinct workstreams divide labor at the peer level; subagents are how a single actor parallelises within its own scope.

## What this site covers

The pages here split into four groups, in roughly the order most readers want them:

- **[Getting started](getting-started/)** — install actor.sh, register the skill and MCP, and walk through your first actor end-to-end.
- **[Concepts](concepts/)** — the model: actors and worktrees, roles, hooks, ask blocks, channel notifications.
- **[Guides](guides/)** — task-shaped configuration topics: Claude and Codex agent setup, the full settings.kdl tour, theming the watch dashboard.
- **[Reference](reference/)** — flat lookup for every CLI subcommand, MCP tool, and config key.

If you're new, [Installation](getting-started/installation/) is the next step.
