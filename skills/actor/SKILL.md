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

1. **Always run `actor run` in background mode.** Use the Bash tool's `run_in_background: true` parameter. Do NOT use shell `&`. You will be automatically notified when the actor finishes.
2. **ALWAYS read the output when an actor finishes.** When you receive a background task notification, you MUST read the output file BEFORE taking any further action or responding to the user. The agent may have asked a question, proposed a plan, or reported an error. You cannot know what happened without reading the output.
3. **Do NOT use `actor logs` for routine output.** The background notification output file is your primary source. Only use `actor logs` when the user explicitly asks for logs or you need historical context.
4. **Choose descriptive actor names.** The name becomes the git branch. Use lowercase with hyphens: `fix-auth`, `refactor-nav`, `add-tests`.
5. **One actor per independent task.** If the user asks for multiple things that don't depend on each other, create multiple actors.
6. **Stay responsive.** After starting actors, tell the user they're running and continue the conversation. Read and report results when the background notification arrives.
7. **Only check status when asked.** Do not proactively run `actor list`, `actor show`, or `actor logs` unless the user asks about status or details.

## Commands Reference

### Create and run an actor
```bash
ACTOR new <name>                          # worktree from current repo (default)
ACTOR new <name> --no-worktree            # run in current directory
ACTOR new <name> --dir /path/to/repo      # worktree from another repo
ACTOR new <name> --base develop           # branch off a specific branch
ACTOR new <name> --config model=sonnet    # set agent config
ACTOR run <name> "<prompt>"             # ALWAYS use run_in_background: true
```

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
ACTOR run <name> "<follow-up prompt>"   # resumes session (run_in_background: true)
```

### Finish actors
```bash
ACTOR done <name>                         # keep branch, clean up
ACTOR done <name> --merge                 # merge into base branch
ACTOR done <name> --pr                    # create a pull request
ACTOR done <name> --pr --title "Fix X"    # PR with custom title
ACTOR done <name> --discard               # delete branch and changes
```

## Workflow Examples

### User: "spin up an actor to refactor the auth module"
```bash
ACTOR new refactor-auth
ACTOR run refactor-auth "Refactor the auth module. Simplify the token validation logic, remove dead code, and make sure all tests pass."
```

### User: "start three actors: fix the nav, update the tests, and rewrite the README"
```bash
ACTOR new fix-nav
ACTOR new update-tests
ACTOR new rewrite-readme
ACTOR run fix-nav "Fix the navigation bar — it's broken on mobile viewports"
ACTOR run update-tests "Update all test files to use the new test utilities"
ACTOR run rewrite-readme "Rewrite the README with proper setup instructions and examples"
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
ACTOR done fix-nav --pr
```

### User: "start an actor on the backend repo to fix the API"
```bash
ACTOR new fix-api --dir /path/to/backend-repo
ACTOR run fix-api "Fix the /users API endpoint — it returns 500 on missing email field"
```

## Crafting Prompts for Actors

Be explicit about what you expect from the actor. Actors are autonomous agents — they will ask questions if the task is ambiguous unless you tell them not to.

- **When the actor should just go build:** End the prompt with "Do not ask questions. Just implement it." or "Go ahead and build this without asking for clarification."
- **When questions are welcome:** Say "If anything is unclear, stop and describe what you need clarification on." or simply leave the prompt open-ended.

Choose based on context. If the user gave clear requirements, tell the actor to just build. If the task is exploratory, let the actor ask.

## Important Notes

- Actors run `claude` under the hood with `--dangerously-skip-permissions` so they can work autonomously.
- Each actor gets its own git worktree by default, so multiple actors can work on the same repo without conflicts.
- Actor sessions persist — you can `actor run` multiple prompts against the same actor and it remembers context.
- If an actor errors, check `ACTOR logs <name> --verbose` to see what went wrong, then `ACTOR run <name> "fix the issue"` to retry.
- When the user says something like "kick off", "spin up", "start", "launch", or "create an actor" — that means `actor new` + `actor run`.
