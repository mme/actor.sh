---
title: "MCP Tool Reference"
description: "Every tool the actor MCP exposes, with arguments, defaults, and behavior."
weight: 2
slug: "mcp-tools"
---

The actor MCP server exposes nine tools to the orchestrator. From a Claude Code perspective each one is named `mcp__actor__<tool>`. This page is the contract: arguments, defaults, return shape, and any constraints.

## How runs report back

`new_actor` and `run_actor` both return immediately. The actual run executes in a background thread; when it finishes the server pushes a `notifications/claude/channel` event through the live MCP session. The orchestrator sees that event as `<channel source="actor" ...>` and reports the result to the user.

The notification's `content` is the run's final output (or a status fallback like `Finished with status: error.`). The `meta` dict carries `{actor: <name>, status: <resolved-status>}`. When the actor was discarded mid-run the row no longer exists, so `status` reports the literal string `discarded` — it isn't a regular status enum value.

See [Channel notifications](../../concepts/05-channel-notifications/) for the full event shape and the orchestrator's contract.

### Tool-call constraints

Each `new_actor` and `run_actor` invocation MUST be its own tool call. Do not batch multiple actor launches into a single call — the channel notification stream pairs one notification with one call, so batching loses the per-actor wiring the orchestrator depends on.

Read-only tools (`list_actors`, `show_actor`, `logs_actor`, `list_roles`, `config_actor` with no pairs) can be called freely.

## list_actors

List all actors and their statuses.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | string | `null` | Optional filter, e.g. `"running"`, `"done"`, `"error"`. |

Returns the same text table the CLI's `actor list` prints.

## list_roles

List the roles defined in settings.kdl. Includes the built-in `main` role plus any user-level and project-level overrides.

No arguments. Returns text. Use this before calling `new_actor` with a `role` argument so the choice is grounded in what's actually defined.

## show_actor

Full detail for one actor: agent, dir, status, saved config, recent runs.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Actor name. |
| `runs` | int | `5` | Number of recent runs to include. `0` to omit run history. |

## logs_actor

Agent session output. The terse view (default) shows prompts and assistant responses; verbose adds tool calls, thinking blocks, and timestamps.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Actor name. |
| `verbose` | bool | `false` | Include tool calls, thinking, and timestamps. |

## stop_actor

Kill the running agent for an actor. The actor row stays in place — only the live process is terminated.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Actor name. |

## discard_actor

Remove an actor from the database. Stops it first if running, then runs the `on-discard` hook, then deletes the row. The worktree stays on disk and the git branch is left in place; clean those up by hand if you want the name back.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Actor name. |
| `force` | bool | `false` | Bypass `on-discard` hook failure (discard even if the hook exits non-zero). |

## config_actor

View or update an actor's saved config.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Actor name. |
| `pairs` | string[] | `[]` | Config `KEY=VALUE` pairs to set. Omit to view. |

Updates take effect on the next run.

## new_actor

Create a new actor. If `prompt` is given, also runs it in the background.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Actor name (becomes the git branch). Lowercase with hyphens. |
| `prompt` | string | `null` | Optional **task** prompt. Distinct from the role's `prompt`, which is the system prompt — omit this to create the actor idle and run it later. |
| `agent` | `"claude"` \| `"codex"` | role's agent or `"claude"` | Coding agent. |
| `role` | string | `null` | Apply a named role from settings.kdl. Use `list_roles` to see what's defined. |
| `dir` | string | orchestrator cwd | Base directory for the worktree. **Use absolute paths.** Relative paths resolve against the MCP server's cwd, which is fragile across sessions. |
| `base` | string | current branch | Branch to create the worktree from. |
| `no_worktree` | bool | `false` | Skip worktree creation. |
| `config` | string[] | `[]` | Agent-arg `KEY=VALUE` pairs (e.g. `["model=opus", "effort=max"]`). Saved as actor defaults. Actor-keys like `use-subscription` are rejected here — use the dedicated parameter. |
| `use_subscription` | bool | `null` | When `true`, strip the agent's API key env var. When `false`, pass it through. When omitted, defer to lower-precedence layers (role / kdl defaults / class default). |

If a `prompt` is supplied, the run kicks off in a background thread and a channel notification fires when it completes. Without a prompt, the actor is created idle.

## run_actor

Run an existing actor. Returns immediately; the channel notification fires on completion.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | string | required | Actor name. |
| `prompt` | string | required | The task. Stripped before use; an empty/whitespace prompt errors. |
| `config` | string[] | `[]` | Per-run agent-arg overrides. **Not saved** — use `config_actor` to change defaults. Actor-keys are rejected here. |
| `use_subscription` | bool | `null` | Per-run actor-key override. When omitted, use the actor's stored value. |

## See also

- [CLI reference](../01-cli/) — the same surface from the terminal side.
- [Config keys](../03-config-keys/) — what's legal in `config=[...]`.
- [Channel notifications](../../concepts/05-channel-notifications/) — how completion events get back to the orchestrator.
- [Ask blocks](../../concepts/04-ask-blocks/) — how `new_actor`, `run_actor`, and `discard_actor` tool descriptions get extra guidance from settings.kdl.
