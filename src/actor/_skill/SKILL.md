---
name: actor
description: Manage coding agents running tasks in parallel. Use when the user wants to start, monitor, or finish background coding tasks — e.g. "spin up an actor to fix the auth module", "start three actors", "what are my actors doing", "make a PR for that actor".
allowed-tools: mcp__actor__list_actors mcp__actor__show_actor mcp__actor__logs_actor mcp__actor__stop_actor mcp__actor__discard_actor mcp__actor__config_actor mcp__actor__new_actor mcp__actor__run_actor Bash(actor *)
---

# Actor — Parallel Coding Agent Orchestrator

<!-- BEGIN AUTO-UPDATED BY actor setup/update -->
<!-- END AUTO-UPDATED BY actor setup/update -->

You are an orchestrator that manages multiple coding agents running in parallel. Each agent runs in its own git worktree (by default) and has its own session that persists across runs.

## MCP is required for a good experience

This skill is designed around the `mcp__actor__*` tools. They return immediately and emit a channel notification when the actor finishes, so you can hand off work and continue the conversation. Shell-only fallback exists but has no completion notifications — it's a last resort.

**If `mcp__actor__*` tools are NOT in your tool list, stop and tell the user how to set it up.** Don't try to install or configure it yourself — the user needs to run these steps:

> The actor MCP server isn't connected to this session. To set it up:
>
> 1. Install the `actor` package (skip if `actor --version` already works):
>    ```
>    uv tool install actor-sh
>    ```
>    (or `pip install actor-sh`)
>
> 2. Register the MCP with your coding agent:
>    ```
>    actor setup --for claude-code
>    ```
>    Optional flags: `--scope project` to install at project level instead of user-wide, `--name <id>` to register under a different name.
>
> 3. Launch a new session with:
>    ```
>    actor claude
>    ```
>    (this enables channel notifications so the session learns when actors finish.)

Only fall back to the CLI (see [cli.md](cli.md)) if the user explicitly prefers to skip MCP setup. When using the CLI fallback, completion is not pushed — you won't know when an actor finishes without asking.

## Agent compatibility

- **Claude Code (via MCP):** fully supported. Channel notifications flow back into the conversation on actor completion.
- **Codex (via MCP):** **NOT currently supported.** Codex does not forward MCP server notifications into the model's conversation — tracked in [openai/codex#17543](https://github.com/openai/codex/issues/17543) and [#18056](https://github.com/openai/codex/issues/18056). Actors spawned from Codex would finish silently; the model would never know. Tell the user to use Claude Code for actor.sh, or wait until Codex ships MCP notification forwarding.
- **Other MCP-capable agents:** works if the agent routes custom notifications to the model's conversation. Check your host's docs.

## Core Rules

1. **Actor runs are background work.** `new_actor` and `run_actor` return immediately; a channel notification arrives when each run completes. React when it arrives.
2. **Each actor run MUST be its own tool call.** Never batch multiple `new_actor` / `run_actor` calls in one combined tool use — completion notifications get mis-routed.
3. **ALWAYS read the notification when an actor finishes.** The actor may have asked a question, proposed a plan, or reported an error. You cannot know what happened without reading the notification body.
4. **Do NOT use `logs_actor` for routine output.** The finish notification is your primary source. Only call `logs_actor` when the user explicitly asks or you need historical context.
5. **Choose descriptive actor names.** The name becomes the git branch. Use lowercase with hyphens: `fix-auth`, `refactor-nav`, `add-tests`.
6. **One actor per independent task.** Multiple parallel asks → multiple actors.
7. **Use worktrees by default in git repos.** Each actor gets its own checkout so parallel work doesn't collide. Only pass `no_worktree=True` when the user explicitly asks or the directory is not a git repo.
8. **Stay responsive.** Tell the user the actors are running and continue the conversation. Report results when the notification arrives.
9. **Only check status when asked.** Don't proactively `list_actors` / `show_actor` / `logs_actor` unless the user asks.

## Commands Reference

### Create and run an actor

Pass a prompt to create and run in one step.

```
new_actor(name="fix-nav", prompt="Fix the nav bar — broken on mobile")
new_actor(name="fix-nav", prompt="...", agent="codex")                      # Codex actor
new_actor(name="fix-nav", prompt="...", base="develop")                     # branch off develop
new_actor(name="fix-nav", prompt="...", dir="/path/to/repo")                # worktree from another repo
new_actor(name="fix-nav", prompt="...", no_worktree=True)                   # no worktree
new_actor(name="fix-nav", prompt="...", config=["model=opus"])              # saved defaults
```

### Create without running

```
new_actor(name="fix-nav")
```

### Run an existing actor

```
run_actor(name="fix-nav", prompt="continue fixing")
run_actor(name="fix-nav", prompt="...", config=["model=opus"])              # per-run override
```

### Change actor configuration

Config changes take effect on the NEXT run — they don't affect an in-flight run. Structural properties (agent, worktree, dir, base branch) are fixed at creation and can't be changed.

```
config_actor(name="fix-nav")                                                # view
config_actor(name="fix-nav", pairs=["model=opus"])                          # update
```

Config reference by actor's agent:
- [Claude config](claude-config.md)
- [Codex config](codex-config.md)

### Monitor

```
list_actors()
list_actors(status="running")
show_actor(name="fix-nav")
show_actor(name="fix-nav", runs=20)
logs_actor(name="fix-nav")
logs_actor(name="fix-nav", verbose=True)                                    # include tool calls, thinking
```

### Stop / discard

```
stop_actor(name="fix-nav")
discard_actor(name="fix-nav")                                               # worktree stays on disk
```

### Interactive sessions

Live Claude / Codex terminal sessions can be embedded in `actor watch`:

- In the watch tree, select an actor and press **Enter** — the detail pane swaps to an embedded terminal running `claude --resume <session_id>` (or `codex resume <session_id>`) in the actor's worktree.
- The actor must not be RUNNING and must already have a session (i.e. it's been run at least once).
- **Ctrl+Z** leaves interactive mode but keeps the subprocess alive. Selecting a different actor in the tree shows that actor's logs (or its own live terminal if it also has one); coming back restores the session.
- Quitting watch kills all live subprocesses and marks their runs STOPPED.
- Each interactive session creates a Run with prompt `*interactive*` so it shows up in `show_actor` and `logs_actor` alongside normal runs.

From the CLI: `actor run <name> -i` does the same thing but uses your existing TTY (no embedded widget).

## Workflow Examples

### User: "spin up an actor to refactor the auth module"
`new_actor(name="refactor-auth", prompt="Refactor the auth module. Simplify the token validation logic, remove dead code, and make sure all tests pass.")`

### User: "start three actors: fix the nav, update the tests, and rewrite the README"
Spawn each in its own tool call:
- `new_actor(name="fix-nav", prompt="Fix the navigation bar — it's broken on mobile viewports")`
- `new_actor(name="update-tests", prompt="Update all test files to use the new test utilities")`
- `new_actor(name="rewrite-readme", prompt="Rewrite the README with proper setup instructions and examples")`

### User: "what are my actors doing?"
`list_actors()` — then summarize the status.

### User: "what did fix-nav do?"
`logs_actor(name="fix-nav")` — then summarize the key actions and results.

### User: "fix-nav looks good, make a PR"
`run_actor(name="fix-nav", prompt="Push your branch and create a pull request against main using gh pr create. Write a clear title and description based on what you did. Report the PR URL when done.")`

After the actor finishes and reports the PR URL:
`discard_actor(name="fix-nav")`

### User: "merge fix-nav into main"
`run_actor(name="fix-nav", prompt="Merge main into your branch to check for conflicts, resolve any issues, then merge your branch into main and push.")`
After finish: discard.

### Forking an actor (trying a different approach)
1. `run_actor(name="feature", prompt="Commit all your changes with a descriptive message.")`
2. `new_actor(name="feature-v2", base="feature", prompt="Take a different approach to...")`

### User: "start a codex actor to fix the API"
`new_actor(name="fix-api", agent="codex", prompt="Fix the /users API endpoint — it returns 500 on missing email field")`

### User: "start an actor on the backend repo to fix the API"
`new_actor(name="fix-api", dir="/path/to/backend-repo", prompt="Fix the /users API endpoint — it returns 500 on missing email field")`

## Crafting Prompts for Actors

Be explicit about what you expect. Actors are autonomous — they'll ask questions if the task is ambiguous unless you tell them not to.

- **Just build:** end with "Do not ask questions. Just implement it."
- **Questions welcome:** "If anything is unclear, stop and describe what you need clarification on."

Choose based on context.

## Important Notes

- Actors run with full permissions by default (`--dangerously-skip-permissions` for Claude, `--dangerously-bypass-approvals-and-sandbox` for Codex). Change via config — see the agent config reference.
- Each actor gets its own git worktree by default so parallel actors don't conflict.
- Actor sessions persist — multiple runs against the same actor keep context.
- If an actor errors, check verbose logs (`logs_actor(name=..., verbose=True)`) and retry with `run_actor`.
- When the user says "kick off", "spin up", "start", "launch", or "create an actor" — that means `new_actor(name=..., prompt=...)`.
