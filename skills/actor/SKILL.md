---
name: actor
description: Manage coding agents running tasks in parallel. Use when the user wants to start, monitor, or finish background coding tasks — e.g. "spin up an actor to fix the auth module", "start three actors", "what are my actors doing", "make a PR for that actor".
allowed-tools: mcp__actor__list_actors mcp__actor__show_actor mcp__actor__logs_actor mcp__actor__stop_actor mcp__actor__discard_actor mcp__actor__config_actor mcp__actor__new_actor mcp__actor__run_actor Bash(actor *)
---

# Actor — Parallel Coding Agent Orchestrator

You are an orchestrator that manages multiple coding agents running in parallel. Each agent runs in its own git worktree (by default) and has its own session that persists across runs.

## Preferred interface

**If you have `mcp__actor__*` tools available, use them.** They return immediately, emit a channel notification when the actor finishes, and accept structured arguments.

If the MCP server isn't connected, fall back to the `actor` CLI. Both surfaces expose the same operations.

## Core Rules

1. **Actor runs are background work.** You get notified when they finish. Never wait synchronously.
   - **MCP:** `new_actor` / `run_actor` already return immediately; a channel notification arrives when the run completes.
   - **CLI:** call `actor new` / `actor run` with the Bash tool's `run_in_background: true` (NOT shell `&`). **Each run MUST be its own Bash tool call** — never batch multiple `actor` commands in one Bash call, or notifications get lost.
2. **ALWAYS read the output when an actor finishes.** The actor may have asked a question, proposed a plan, or reported an error. You cannot know what happened without reading the output (channel notification body for MCP, background output file for CLI).
3. **Do NOT use `actor logs` / `logs_actor` for routine output.** The finish-notification is your primary source. Only use logs when the user explicitly asks or you need historical context.
4. **Choose descriptive actor names.** The name becomes the git branch. Use lowercase with hyphens: `fix-auth`, `refactor-nav`, `add-tests`.
5. **One actor per independent task.** Multiple parallel asks → multiple actors.
6. **Use worktrees by default in git repos.** Each actor gets its own checkout so parallel work doesn't collide. Only pass `no_worktree=True` / `--no-worktree` when the user explicitly asks or the directory is not a git repo.
7. **Stay responsive.** Tell the user the actors are running and continue the conversation. Report results when the notification arrives.
8. **Only check status when asked.** Don't proactively `list_actors` / `actor list` / `show_actor` / `actor show` / `logs_actor` / `actor logs` unless the user asks.

## Commands Reference

Each operation is shown in both forms. Use MCP when available.

### Create and run an actor

Pass a prompt to create and run in one step.

**MCP:**
```
new_actor(name="fix-nav", prompt="Fix the nav bar — broken on mobile")
new_actor(name="fix-nav", prompt="...", agent="codex")                      # Codex agent
new_actor(name="fix-nav", prompt="...", base="develop")                     # branch off develop
new_actor(name="fix-nav", prompt="...", dir="/path/to/repo")                # worktree from another repo
new_actor(name="fix-nav", prompt="...", no_worktree=True)                   # no worktree
new_actor(name="fix-nav", prompt="...", config=["model=opus"])              # saved defaults
```

**CLI:**
```bash
actor new fix-nav "Fix the nav bar — broken on mobile"
actor new fix-nav --agent codex "..."
actor new fix-nav --base develop "..."
actor new fix-nav --dir /path/to/repo "..."
actor new fix-nav --no-worktree "..."
actor new fix-nav --config model=opus "..."
actor new fix-nav --no-strip-api-keys "..."                                 # pass API keys through
echo "fix it" | actor new fix-nav                                           # prompt from stdin
```

### Create without running

**MCP:** `new_actor(name="fix-nav")` (omit `prompt`)
**CLI:** `actor new fix-nav`

### Run an existing actor

Resumes the actor's session with a new prompt.

**MCP:**
```
run_actor(name="fix-nav", prompt="continue fixing")
run_actor(name="fix-nav", prompt="...", config=["model=opus"])              # per-run override
```

**CLI:**
```bash
actor run fix-nav "continue fixing"
actor run fix-nav --config model=opus "..."                                 # per-run override
```

### Change actor configuration

Config changes take effect on the NEXT run — they don't affect an in-flight run. Structural properties (agent, worktree, dir, base branch) are set at creation and can't be changed.

**MCP:**
```
config_actor(name="fix-nav")                                                # view
config_actor(name="fix-nav", pairs=["model=opus"])                          # update
```

**CLI:**
```bash
actor config fix-nav                                                        # view
actor config fix-nav model=opus                                             # update
actor config fix-nav model=sonnet effort=max                                # multiple at once
```

Config reference by agent:
- [Claude config](claude-config.md)
- [Codex config](codex-config.md)

### Monitor

**MCP:**
```
list_actors()
list_actors(status="running")
show_actor(name="fix-nav")                                                  # details + last 5 runs
show_actor(name="fix-nav", runs=20)
logs_actor(name="fix-nav")
logs_actor(name="fix-nav", verbose=True)                                    # include tool calls, thinking
```

**CLI:**
```bash
actor list
actor list --status running
actor show fix-nav
actor show fix-nav --runs 20
actor logs fix-nav
actor logs fix-nav --verbose
```

### Stop / discard

**MCP:**
```
stop_actor(name="fix-nav")
discard_actor(name="fix-nav")                                               # worktree stays on disk
```

**CLI:**
```bash
actor stop fix-nav
actor discard fix-nav
```

## Workflow Examples

### User: "spin up an actor to refactor the auth module"
MCP: `new_actor(name="refactor-auth", prompt="Refactor the auth module. Simplify the token validation logic, remove dead code, and make sure all tests pass.")`
CLI: `actor new refactor-auth "Refactor the auth module. ..."`

### User: "start three actors: fix the nav, update the tests, and rewrite the README"
Spawn each in its own tool call (parallel is fine — just not batched in a single Bash call):
- `new_actor(name="fix-nav", prompt="Fix the navigation bar — it's broken on mobile viewports")`
- `new_actor(name="update-tests", prompt="Update all test files to use the new test utilities")`
- `new_actor(name="rewrite-readme", prompt="Rewrite the README with proper setup instructions and examples")`

### User: "what are my actors doing?"
`list_actors()` / `actor list` — then summarize the status.

### User: "what did fix-nav do?"
`logs_actor(name="fix-nav")` / `actor logs fix-nav` — then summarize the key actions and results.

### User: "fix-nav looks good, make a PR"
`run_actor(name="fix-nav", prompt="Push your branch and create a pull request against main using gh pr create. Write a clear title and description based on what you did. Report the PR URL when done.")`

After the actor finishes and reports the PR URL:
`discard_actor(name="fix-nav")` / `actor discard fix-nav`

### User: "merge fix-nav into main"
`run_actor(name="fix-nav", prompt="Merge main into your branch to check for conflicts, resolve any issues, then merge your branch into main and push.")`
After finish: discard.

### Forking an actor (trying a different approach)
Have the actor commit first, then create a sibling from its branch:
1. `run_actor(name="feature", prompt="Commit all your changes with a descriptive message.")`
2. `new_actor(name="feature-v2", base="feature", prompt="Take a different approach to...")`

### User: "start a codex actor to fix the API"
`new_actor(name="fix-api", agent="codex", prompt="Fix the /users API endpoint — it returns 500 on missing email field")`

### User: "start an actor on the backend repo to fix the API"
`new_actor(name="fix-api", dir="/path/to/backend-repo", prompt="Fix the /users API endpoint — it returns 500 on missing email field")`

## Crafting Prompts for Actors

Be explicit about what you expect. Actors are autonomous — they'll ask questions if the task is ambiguous unless you tell them not to.

- **Just build:** end with "Do not ask questions. Just implement it." or "Go ahead and build this without asking for clarification."
- **Questions welcome:** "If anything is unclear, stop and describe what you need clarification on." or leave the prompt open-ended.

Choose based on context. If the user gave clear requirements, tell the actor to just build. If the task is exploratory, let the actor ask.

## Important Notes

- Actors run with full permissions by default (`--dangerously-skip-permissions` for Claude, `--dangerously-bypass-approvals-and-sandbox` for Codex). Change via config — see the agent config reference.
- Each actor gets its own git worktree by default so parallel actors don't conflict.
- Actor sessions persist — multiple runs against the same actor keep context.
- If an actor errors, check verbose logs (`logs_actor(name=..., verbose=True)` / `actor logs <name> --verbose`) and retry with `run_actor` / `actor run`.
- When the user says "kick off", "spin up", "start", "launch", or "create an actor" — that means `new_actor(name=..., prompt=...)` / `actor new <name> "<prompt>"`.
