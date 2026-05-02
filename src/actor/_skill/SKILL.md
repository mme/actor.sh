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

### Roles

Roles are named bundles of actor defaults. They live in KDL files that
the user edits directly — there is no `actor init` command, so use the
Read / Write / Edit tools when the user asks to view, add, or change a
role.

**File locations** (both optional; create whichever fits the user's ask):

- `~/.actor/settings.kdl` — user-wide. Applies to every repo.
- `<project>/.actor/settings.kdl` — project-scoped. Found by walking up
  from the current directory (git-style), so any cwd inside the repo sees
  it.

**Precedence:** when the same role name appears in both files, the
project file wins. Users can set a baseline role in `~/.actor/` and
override it per-repo.

**Role block syntax:**

```kdl
role "qa" {
    description "Run tests after changes; report failures concisely."
    agent "claude"
    model "opus"
    effort "max"
    use-subscription true
    prompt "You're a QA engineer. Run the tests, report what fails."
}

role "reviewer" {
    description "Concise code review; flag bugs and style issues."
    agent "claude"
    model "sonnet"
    prompt "You're a code reviewer. Be concise."
}
```

**Valid keys inside a role:**

- `agent` (string) — `"claude"` or `"codex"`. Sets which CLI the actor runs.
- `prompt` (string) — default prompt used when the user doesn't pass one
  on the CLI.
- `description` (string, optional) — short "when to use this role" line.
  Surfaced by `actor roles` (CLI) and `mcp__actor__list_roles` (MCP) so
  you can pick the right role without re-reading settings.kdl.
- Any key from the agent's config reference ([claude-config.md](claude-config.md),
  [codex-config.md](codex-config.md)) — e.g. `model`, `effort`,
  `use-subscription`, `max-budget-usd`. Values may be strings, booleans, or
  numbers; they're all coerced to strings to match the actor config
  pipeline.

**Discover what's defined.** Call `mcp__actor__list_roles` (MCP) or run
`actor roles` (CLI) before applying a role — both print the same table
of name, agent, and description, drawn live from the merged
user+project settings.kdl. A built-in `main` role (claude + a short
system prompt) is always present and can be overridden by a `role
"main" { ... }` block in settings.kdl.

Unknown top-level nodes (`alias`) parse as no-ops today — they're
reserved for follow-up tickets. Malformed KDL raises an error with the
file path.

**Lifecycle hooks** run shell commands around actor events (create,
run, discard) via an optional top-level `hooks { }` block. Each value
runs via `/bin/sh -c` with `ACTOR_NAME`, `ACTOR_DIR`, `ACTOR_AGENT`,
and `ACTOR_SESSION_ID` (when set) in the env; cwd is the actor's
worktree:

```kdl
hooks {
    on-start   "./scripts/setup.sh"
    before-run "git fetch --quiet"
    after-run  "./scripts/notify.sh"
    on-discard "git diff --quiet"
}
```

- `on-start` — fires once during `actor new`. Non-zero rolls back
  the actor.
- `before-run` — fires before every `actor run` (incl. interactive).
  Non-zero aborts the run with no DB row written.
- `after-run` — fires after the run finishes and the DB row has
  been updated with final status. Receives `ACTOR_RUN_ID`,
  `ACTOR_EXIT_CODE`, `ACTOR_DURATION_MS`. Observer only — non-zero
  exit logs a warning but does not fail the completed run.
- `on-discard` — fires during `actor discard`. Non-zero aborts
  discard unless the user runs `actor discard --force` (CLI) or
  passes `force=True` to `discard_actor` (MCP).

Project hooks override user hooks per event.

**Per-agent defaults** live alongside roles and apply automatically
to every new actor of that agent kind:

```kdl
defaults "claude" {
    use-subscription true
    permission-mode "auto"
    model "opus"
}

defaults "codex" {
    m "o3"
    sandbox "workspace-write"
}
```

All keys live in one flat namespace. Each key is routed at parse time
by checking the agent class's `ACTOR_DEFAULTS` whitelist:

- **Whitelisted keys** (today only `use-subscription` for both agents)
  are actor-sh controls — `use-subscription` strips `ANTHROPIC_API_KEY`
  / `OPENAI_API_KEY` from the child env so the subscription login is
  used instead of the API key.
- **Everything else** maps directly to the agent binary's CLI flags.
  Claude uses semantic long flags (`model`, `permission-mode`). Codex
  uses whatever flag names `codex` itself accepts — `-m` / `-a`
  (short), `--sandbox` / `--config` (long). No translation layer on
  either side.
- **`null` cancels a lower-precedence value.** For example, a project
  file can set `permission-mode null` under `defaults "claude"` to
  erase a user-level default without forcing a replacement.

Precedence at `actor new` (low → high): class-level hardcoded defaults →
user kdl → project kdl → role → CLI `--config`. The resolved merge
is snapshotted onto the actor at creation time; later kdl edits don't
retroactively mutate existing actors (use `actor config <name>` for
that).

Built-in class defaults (no kdl file needed):
- Claude: `use-subscription "true"`, `permission-mode "auto"`.
- Codex: `use-subscription "true"`, `sandbox "danger-full-access"`,
  `a "never"`.

**Applying a role** (CLI only — see note below):

```bash
actor new fix-auth --role qa                          # apply role
actor new fix-auth --role qa --config model=haiku     # CLI overrides role
actor new fix-auth --role qa --agent codex            # CLI agent beats role
actor new fix-auth --role qa "custom prompt"          # CLI prompt beats role
```

Explicit CLI flags (`--agent`, `--model`, `--config`, positional prompt,
stdin) always beat the role's values. If the role name is wrong, the
error lists the available names.

**MCP equivalent:** `mcp__actor__new_actor(name=..., role="qa", ...)`
applies a role the same way; `mcp__actor__list_roles` discovers them.
Per-call `prompt` / `config` / `agent` / `use_subscription` parameters
beat the role's values.

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

- Actors run autonomous-leaning by default — Claude uses `permission-mode "auto"` (agent decides when to ask; effectively autonomous inside a worktree), Codex uses `sandbox "danger-full-access"` + `a "never"` (truly unrestricted). To fully bypass Claude's permission checks, set `permission-mode "bypassPermissions"`. See the agent config reference for other options.
- Each actor gets its own git worktree by default so parallel actors don't conflict.
- Actor sessions persist — multiple runs against the same actor keep context.
- If an actor errors, check verbose logs (`logs_actor(name=..., verbose=True)`) and retry with `run_actor`.
- When the user says "kick off", "spin up", "start", "launch", or "create an actor" — that means `new_actor(name=..., prompt=...)`.
