---
name: actor
description: Manage coding agents running tasks in parallel. Use when the user wants to start, monitor, or finish background coding tasks — e.g. "spin up an actor to fix the auth module", "start three actors", "what are my actors doing", "make a PR for that actor".
allowed-tools: Bash(*/actor.sh *)
---

# Actor — Parallel Coding Agent Orchestrator

You are an orchestrator that manages multiple coding agents running in parallel. Each agent runs in its own git worktree (by default) and has its own session that persists across runs.

## Setup

The actor command (requires Python 3.9+):

```!
echo "${CLAUDE_SKILL_DIR}/actor.sh"
```

Refer to this as `ACTOR` in the instructions below. Use the exact path printed above for all commands.

## Core Rules

1. **Always run `actor run` in background mode.** Use the Bash tool's `run_in_background: true` parameter. Do NOT use shell `&`. You will be automatically notified when the actor finishes. **Each `actor run` MUST be its own separate Bash tool call.** Never batch multiple `actor run` commands in a single Bash call — this prevents proper process tracking and notification.
2. **ALWAYS read the output when an actor finishes.** When you receive a background task notification, you MUST read the output file BEFORE taking any further action or responding to the user. The agent may have asked a question, proposed a plan, or reported an error. You cannot know what happened without reading the output.
3. **Do NOT use `actor logs` for routine output.** The background notification output file is your primary source. Only use `actor logs` when the user explicitly asks for logs or you need historical context.
4. **Choose descriptive actor names.** The name becomes the git branch. Use lowercase with hyphens: `fix-auth`, `refactor-nav`, `add-tests`.
5. **One actor per independent task.** If the user asks for multiple things that don't depend on each other, create multiple actors.
6. **Use worktrees by default in git repos.** When in a git repository, always create actors with worktrees (the default). This is critical when running multiple actors — each gets its own isolated copy of the repo so they don't overwrite each other's changes. Only use `--no-worktree` when the user explicitly asks or the directory is not a git repo.
7. **Stay responsive.** After starting actors, tell the user they're running and continue the conversation. Read and report results when the background notification arrives.
8. **Only check status when asked.** Do not proactively run `actor list`, `actor show`, or `actor logs` unless the user asks about status or details.

## Commands Reference

### Run an actor
```bash
ACTOR run <name> -c "<prompt>"            # create and run (worktree from current repo)
ACTOR run <name> "<prompt>"               # run existing actor (resumes session)
ACTOR run <name> -c --model opus "<prompt>"       # create with specific model
ACTOR run <name> -c --agent codex "<prompt>"      # create with Codex agent
ACTOR run <name> -c --base develop "<prompt>"     # create from specific branch
ACTOR run <name> -c --dir /path/to/repo "<prompt>"  # create from another repo
ACTOR run <name> -c --no-worktree "<prompt>"      # create without worktree
ACTOR run <name> -i                       # resume interactively (not for skill use)
```

The `-c` flag creates the actor before running. Without `-c`, the actor must already exist.

**Prompt:** Pass as an argument or pipe via stdin (`echo "fix it" | ACTOR run name -c`).

**Flags:**
- `--model` sets the model. On `-c` it's saved for all runs; without `-c` it overrides this run only.
- `--agent` selects the coding agent: `claude` (default), `codex`. Requires `-c`.
- `--strip-api-keys` (default: on) strips API keys from the environment so agents use subscription auth. Use `--no-strip-api-keys` to pass keys through.
- `--config key=value` sets agent-specific options. Bare keys (no `=`) are boolean flags. See the config reference for your agent:
  - [Claude config](claude-config.md)
  - [Codex config](codex-config.md)

### Monitor actors
```bash
ACTOR list                                # all actors and their status
ACTOR list --status running               # only running actors
ACTOR show <name>                         # full details + run history
ACTOR logs <name>                         # agent output (prompts + responses)
ACTOR logs <name> --verbose               # full output with tool calls, thinking, timestamps
```

### Manage actors
```bash
ACTOR stop <name>                         # kill a running actor
ACTOR config <name>                       # view config
ACTOR config <name> model=opus            # update config
```

### Finish actors
```bash
ACTOR done <name>                         # remove actor from DB (worktree stays on disk)
```

## Workflow Examples

### User: "spin up an actor to refactor the auth module"
```bash
ACTOR run refactor-auth -c "Refactor the auth module. Simplify the token validation logic, remove dead code, and make sure all tests pass."
```

### User: "start three actors: fix the nav, update the tests, and rewrite the README"
```bash
ACTOR run fix-nav -c "Fix the navigation bar — it's broken on mobile viewports"
ACTOR run update-tests -c "Update all test files to use the new test utilities"
ACTOR run rewrite-readme -c "Rewrite the README with proper setup instructions and examples"
```

### User: "what are my actors doing?"
```bash
ACTOR list
```
Then summarize the status for the user.

### User: "what did fix-nav do?"
```bash
ACTOR logs fix-nav
```
Then summarize the key actions and results.

### User: "fix-nav looks good, make a PR"
```bash
ACTOR run fix-nav "Push your branch and create a pull request against main using gh pr create. Write a clear title and description based on what you did. Report the PR URL when done."
```
After the actor finishes and reports the PR URL:
```bash
ACTOR done fix-nav
```

### User: "merge fix-nav into main"
```bash
ACTOR run fix-nav "Merge main into your branch to check for conflicts, resolve any issues, then merge your branch into main and push."
```
After the actor finishes:
```bash
ACTOR done fix-nav
```

### Forking an actor (trying a different approach)
To fork an actor's work into a new direction, have the actor commit first, then create a new actor from its branch:
```bash
ACTOR run feature "Commit all your changes with a descriptive message."
```
After the actor commits:
```bash
ACTOR run feature-v2 -c --base feature "Take a different approach to..."
```

### User: "start a codex actor to fix the API"
```bash
ACTOR run fix-api -c --agent codex "Fix the /users API endpoint — it returns 500 on missing email field"
```

### User: "start an actor on the backend repo to fix the API"
```bash
ACTOR run fix-api -c --dir /path/to/backend-repo "Fix the /users API endpoint — it returns 500 on missing email field"
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
- If an actor errors, check `ACTOR logs <name> --verbose` to see what went wrong, then `ACTOR run <name> "fix the issue"` to retry.
- When the user says something like "kick off", "spin up", "start", "launch", or "create an actor" — that means `actor run -c`.
