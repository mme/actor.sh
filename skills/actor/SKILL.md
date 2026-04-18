---
name: actor
description: Manage coding agents running tasks in parallel. Use when the user wants to start, monitor, or finish background coding tasks — e.g. "spin up an actor to fix the auth module", "start three actors", "what are my actors doing", "make a PR for that actor".
allowed-tools: Bash(actor *)
---

# Actor — Parallel Coding Agent Orchestrator

You are an orchestrator that manages multiple coding agents running in parallel. Each agent runs in its own git worktree (by default) and has its own session that persists across runs.

## Setup

Requires `pip install actor-sh` (Python 3.9+). All commands below use the `actor` CLI.

## Core Rules

1. **Always run `actor new`/`actor run` in background mode.** Use the Bash tool's `run_in_background: true` parameter. Do NOT use shell `&`. You will be automatically notified when the actor finishes. **Each `actor new`/`actor run` with a prompt MUST be its own separate Bash tool call.** Never batch multiple such calls in a single Bash call — this prevents proper process tracking and notification.
2. **ALWAYS read the output when an actor finishes.** When you receive a background task notification, you MUST read the output file BEFORE taking any further action or responding to the user. The agent may have asked a question, proposed a plan, or reported an error. You cannot know what happened without reading the output.
3. **Do NOT use `actor logs` for routine output.** The background notification output file is your primary source. Only use `actor logs` when the user explicitly asks for logs or you need historical context.
4. **Choose descriptive actor names.** The name becomes the git branch. Use lowercase with hyphens: `fix-auth`, `refactor-nav`, `add-tests`.
5. **One actor per independent task.** If the user asks for multiple things that don't depend on each other, create multiple actors.
6. **Use worktrees by default in git repos.** When in a git repository, always create actors with worktrees (the default). This is critical when running multiple actors — each gets its own isolated copy of the repo so they don't overwrite each other's changes. Only use `--no-worktree` when the user explicitly asks or the directory is not a git repo.
7. **Stay responsive.** After starting actors, tell the user they're running and continue the conversation. Read and report results when the background notification arrives.
8. **Only check status when asked.** Do not proactively run `actor list`, `actor show`, or `actor logs` unless the user asks about status or details.

## Commands Reference

### Create and run an actor

Use `actor new` to create a new actor. If you pass a prompt, it also runs immediately.

```bash
actor new <name> "<prompt>"                       # create and run (worktree from current repo)
actor new <name>                                   # create without running
actor new <name> --model opus "<prompt>"           # create with specific model
actor new <name> --agent codex "<prompt>"          # create with Codex agent
actor new <name> --base develop "<prompt>"         # create from specific branch
actor new <name> --dir /path/to/repo "<prompt>"    # create from another repo
actor new <name> --no-worktree "<prompt>"          # create without worktree
echo "fix it" | actor new <name>                   # prompt from stdin
```

### Run an existing actor

Use `actor run` to run an actor that already exists (resumes its session).

```bash
actor run <name> "<prompt>"                                # run with a prompt
actor run <name> --config model=opus "<prompt>"            # one-off per-run config override (not saved)
actor run <name> -i                                        # resume interactively (not for skill use)
echo "fix it" | actor run <name>                           # prompt from stdin
```

**Flags:**
- `--config key=value ...` — per-run config overrides. NOT saved to the actor's defaults. Use `actor config` to change defaults.

### Change actor configuration

Config set at creation (`actor new --model ...`) becomes the actor's defaults. To change defaults later, use `actor config`.

```bash
actor config <name>                                # view config
actor config <name> model=opus                     # update one key
actor config <name> model=sonnet effort=max        # update multiple
```

Config changes apply to the **next** run — they don't affect an in-flight run. Structural properties (agent, worktree, dir, base branch) are set at creation and can't be changed via `config`.

**Config reference by agent:**
- [Claude config](claude-config.md)
- [Codex config](codex-config.md)

### Monitor actors

```bash
actor list                                # all actors and their status
actor list --status running               # only running actors
actor show <name>                         # full details + run history
actor logs <name>                         # agent output (prompts + responses)
actor logs <name> --verbose               # full output with tool calls, thinking, timestamps
```

### Manage actors

```bash
actor stop <name>                         # kill a running actor
```

### Finish actors

```bash
actor discard <name>                      # remove actor from DB (worktree stays on disk)
```

## Workflow Examples

### User: "spin up an actor to refactor the auth module"
```bash
actor new refactor-auth "Refactor the auth module. Simplify the token validation logic, remove dead code, and make sure all tests pass."
```

### User: "start three actors: fix the nav, update the tests, and rewrite the README"
```bash
actor new fix-nav "Fix the navigation bar — it's broken on mobile viewports"
actor new update-tests "Update all test files to use the new test utilities"
actor new rewrite-readme "Rewrite the README with proper setup instructions and examples"
```

### User: "what are my actors doing?"
```bash
actor list
```
Then summarize the status for the user.

### User: "what did fix-nav do?"
```bash
actor logs fix-nav
```
Then summarize the key actions and results.

### User: "fix-nav looks good, make a PR"
```bash
actor run fix-nav "Push your branch and create a pull request against main using gh pr create. Write a clear title and description based on what you did. Report the PR URL when done."
```
After the actor finishes and reports the PR URL:
```bash
actor discard fix-nav
```

### User: "merge fix-nav into main"
```bash
actor run fix-nav "Merge main into your branch to check for conflicts, resolve any issues, then merge your branch into main and push."
```
After the actor finishes:
```bash
actor discard fix-nav
```

### Forking an actor (trying a different approach)
To fork an actor's work into a new direction, have the actor commit first, then create a new actor from its branch:
```bash
actor run feature "Commit all your changes with a descriptive message."
```
After the actor commits:
```bash
actor new feature-v2 --base feature "Take a different approach to..."
```

### User: "start a codex actor to fix the API"
```bash
actor new fix-api --agent codex "Fix the /users API endpoint — it returns 500 on missing email field"
```

### User: "start an actor on the backend repo to fix the API"
```bash
actor new fix-api --dir /path/to/backend-repo "Fix the /users API endpoint — it returns 500 on missing email field"
```

## Crafting Prompts for Actors

Be explicit about what you expect from the actor. Actors are autonomous agents — they will ask questions if the task is ambiguous unless you tell them not to.

- **When the actor should just go build:** End the prompt with "Do not ask questions. Just implement it." or "Go ahead and build this without asking for clarification."
- **When questions are welcome:** Say "If anything is unclear, stop and describe what you need clarification on." or simply leave the prompt open-ended.

Choose based on context. If the user gave clear requirements, tell the actor to just build. If the task is exploratory, let the actor ask.

## Important Notes

- Actors run with full permissions by default (`--dangerously-skip-permissions` for Claude, `--dangerously-bypass-approvals-and-sandbox` for Codex). This can be changed via config — see the agent config reference.
- Each actor gets its own git worktree by default, so multiple actors can work on the same repo without conflicts.
- Actor sessions persist — you can `actor run` multiple prompts against the same actor and it remembers context.
- If an actor errors, check `actor logs <name> --verbose` to see what went wrong, then `actor run <name> "fix the issue"` to retry.
- When the user says something like "kick off", "spin up", "start", "launch", or "create an actor" — that means `actor new <name> "<prompt>"`.
