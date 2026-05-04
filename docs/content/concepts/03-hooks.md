---
title: "Lifecycle hooks"
description: "Shell commands that fire around actor create, run, and discard events."
weight: 3
slug: "hooks"
---

Lifecycle hooks let you run shell commands around the events in an actor's life: creation, before each run, after each run, and on discard. They're declared in a top-level `hooks { }` block in `settings.kdl`, and each value is run via `/bin/sh -c` — so anything you can write in a shell line works.

## The four hooks

```kdl
hooks {
    on-start   "kubectl config use-context dev"
    before-run "git fetch --quiet"
    after-run  "./scripts/notify.sh"
    on-discard "git diff --quiet && git diff --quiet --staged"
}
```

Every hook runs with the actor's worktree as cwd, the caller's environment plus the variables `ACTOR_NAME`, `ACTOR_DIR`, `ACTOR_AGENT`, and (when the actor has a session) `ACTOR_SESSION_ID`.

### `on-start` — fires once during `actor new`

Runs after the actor row is recorded and the worktree is created, before `actor new` returns. A non-zero exit rolls back the actor: the database row is deleted and the worktree is torn down, so the actor never appears.

Use this for one-time setup that must succeed for the actor to be viable: switching cluster contexts, copying secrets into the worktree, installing dependencies.

### `before-run` — fires before every `actor run`

Runs before each run, including interactive ones (`actor run -i`) and the implicit run launched by `actor new <name> "<task>"`. A non-zero exit aborts the run with no `Run` row written to the database — the agent never starts.

Common uses:

```kdl
hooks {
    before-run "git fetch --quiet"
}
```

### `after-run` — fires after the run completes

Runs once the agent process has exited and the run row has been updated with its final status. Receives three extra environment variables: `ACTOR_RUN_ID`, `ACTOR_EXIT_CODE`, `ACTOR_DURATION_MS`.

Unlike the other hooks, `after-run` is **observer-only**. A non-zero exit logs a warning but does not fail the run that just completed — there's nothing to roll back. Use it for notifications, metrics, or post-run checks:

```kdl
hooks {
    after-run "./scripts/notify.sh \"$ACTOR_NAME finished with $ACTOR_EXIT_CODE\""
}
```

### `on-discard` — fires during `actor discard`

Runs after any active agent has been stopped, before the database row is deleted. A non-zero exit aborts the discard unless the user passes `actor discard --force` (CLI) or `force=True` (MCP).

If the worktree directory is already gone (deleted out from under actor.sh), the hook still runs — from `$HOME` instead of the missing path — and `ACTOR_DIR` still reports the absent path so a script can detect it.

The default-friendly check is "no uncommitted work":

```kdl
hooks {
    on-discard "git diff --quiet && git diff --quiet --staged"
}
```

## Precedence

If both `~/.actor/settings.kdl` and `<repo>/.actor/settings.kdl` define a hook, the project value wins **per event**. A user-level `after-run` plus a project-level `before-run` coexist; both fire.
