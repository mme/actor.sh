---
title: "Your first actor"
description: "Launch the orchestrator, spawn an actor, and inspect the work it did."
weight: 3
---

This walk-through takes you from a fresh shell to a finished actor. You'll launch the main actor, ask it to spin up a sub-actor in natural language, then inspect the work and clean up.

## Open the orchestrator

Change into the repository you want to work on and run:

```bash
cd ~/work/myapp
actor main
```

`actor main` execs `claude` with two pieces layered on:

- The built-in `main` role's prompt (the main actor brief) is appended as a system prompt.
- The actor channel is enabled, so completion notifications from sub-actors flow back into the conversation.

You're now in a normal Claude Code session — the only difference is that this one knows how to manage actors and learns about their completion in real time.

## Ask for an actor

Talk to the orchestrator in plain English. For example:

> Spin up an actor to refactor the auth module. Simplify the token validation logic and make sure the tests still pass.

What happens under the hood:

1. The orchestrator picks a descriptive lowercase-with-hyphens name — say `refactor-auth`.
2. It calls `mcp__actor__new_actor` with that name and a prompt derived from your request.
3. actor.sh creates a new git branch and a worktree at `~/.actor/worktrees/refactor-auth/`, then launches a Claude Code sub-agent inside it.
4. The orchestrator returns control to you immediately. The sub-actor keeps working in the background.
5. When the sub-actor finishes, a channel notification arrives in your orchestrator session as a `<channel source="actor" ...>` event. The orchestrator reads it and reports the result.

You can keep talking to the orchestrator while sub-actors are running. Spawn more (`also start a reviewer to look at the API once that's done`), ask about progress, or work on something unrelated. Each `new_actor` and `run_actor` call goes through its own tool invocation so completion events route correctly.

## Inspect from the shell

Sometimes you want to look at the raw state without going through the orchestrator. The CLI mirrors the MCP tools.

### List actors

```bash
actor list
actor list --status running
```

This prints a table with each actor's name, agent, status, and a short status line. Useful as a quick check that something is actually running.

### Show details for one

```bash
actor show refactor-auth
actor show refactor-auth --runs 20
```

`actor show` prints metadata (worktree path, branch, agent, session ID, stored config) plus the most recent runs. `--runs N` controls how many run rows are included; `--runs 0` shows just the metadata.

### Read the agent's session log

```bash
actor logs refactor-auth
actor logs refactor-auth --verbose
actor logs refactor-auth --watch
```

The plain output color-codes user prompts and assistant responses. `--verbose` adds tool calls, thinking, and timestamps — that's the form to use when something went wrong and you need to see exactly what the agent tried. `--watch` streams output live as the agent works.

### See the diff

The actor's branch lives in `~/.actor/worktrees/<name>/`, so you can `cd` there and run normal `git` commands, or use the watch dashboard's Diff tab (see [the watch dashboard](../watch-dashboard/)).

## Discard when you're done

Once you've reviewed the actor's work and either merged its branch or decided to throw it away, drop the actor with:

```bash
actor discard refactor-auth
```

Discard removes the worktree directory and the actor's row from the database. It runs the configured `on-discard` hook first; the default hook checks that there are no uncommitted changes, so committed work or merged branches discard cleanly. If the hook fails (uncommitted edits, for example) and you really do want to discard anyway, pass `--force` / `-f`.

A note on what discard does **not** touch: the underlying git branch stays in place. If you want to reuse the same actor name later, delete the branch in the source repo first (`git branch -D refactor-auth`) or pick a different name.

## The dashboard alternative

Everything above also has a TUI form: [the watch dashboard](../watch-dashboard/) shows the actor list, logs, diffs, and runs in a live master-detail view, and lets you drop into a live Claude or Codex session for any actor. Most users keep `actor watch` open in one window while working with the orchestrator in another.
