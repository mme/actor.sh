---
title: "Actors and worktrees"
description: "An actor is a coding agent running a task on its own git branch in an isolated worktree."
weight: 1
slug: "actors-and-worktrees"
---

An **actor** is a coding agent — `claude` or `codex` — running a task on its own git branch, inside its own working tree. Actors are the unit of work in actor.sh: each one is isolated from your main checkout and from every other actor, so you can run several in parallel without them stepping on each other's edits.

## The worktree model

When you create an actor, actor.sh creates a git worktree at `~/.actor/worktrees/<name>/` on a new branch named after the actor. The branch is forked off your current branch by default; pass `--base develop` (CLI) or `base="develop"` (MCP) to fork off something else. The actor's agent runs with that worktree as its cwd, so every edit it makes — staged or unstaged, committed or not — stays inside its own branch.

```bash
actor new fix-nav "Fix the broken mobile nav"
# creates branch  fix-nav   off the current branch
# worktree at     ~/.actor/worktrees/fix-nav/
```

For directories that aren't git repositories, pass `--no-worktree` (CLI) or `no_worktree=True` (MCP). The actor runs in the original directory with no branch isolation.

## Lifecycle

An actor moves through three commands:

- **`new`** — create the actor: register it in the database, create the worktree, optionally start a first run if a prompt is given.
- **`run`** — run a task on an existing actor. The agent's session is preserved across runs — calling `run_actor` a second time resumes the same Claude or Codex session, so context carries over.
- **`stop`** — interrupt a running agent. The actor stays around; you can `run` it again.
- **`discard`** — remove the worktree and the database row.

Multiple `run_actor` calls against the same actor keep building on the same session — you can hand off work, inspect logs, run again with more guidance.

## Database and runtime paths

State lives at `~/.actor/actor.db`, a SQLite database created on first use. Worktrees live at `~/.actor/worktrees/<name>/`. Claude session logs land in `~/.claude/projects/<encoded-dir>/<session-id>.jsonl` — `logs_actor` reads from there.

## What discard does NOT clean up

`discard` removes the worktree directory and the database row, but **leaves the underlying git branch in place**. This is intentional: the default `on-discard` hook only catches unstaged modifications, so committed work would be silently destroyed if the branch were force-deleted. The trade-off is that `actor new <same-name>` after a discard fails with "branch already exists" until you delete the branch yourself in the source repo (after confirming its commits are merged or unwanted), or pick a different name.

## Parent-child tracking

Actors can spawn other actors. When an agent inside an actor calls `new_actor`, the child inherits the `ACTOR_NAME` env var of its parent and is recorded in the database with a `parent` column pointing back. `discard` cascades: discarding a parent stops and removes its children first, then the parent itself.

See the [settings.kdl tour](../../guides/03-settings-kdl/) for how to customize default behavior, and [roles](../02-roles/) for shaping what an actor does on creation.
