---
title: "CLI Reference"
description: "Every actor subcommand with its flags and a worked example."
weight: 1
---

Complete reference for the `actor` CLI. Each section covers one subcommand: its flags, defaults, and a minimal example. Run `actor --help` or `actor <cmd> --help` for the in-terminal version.

## actor main

Launch the orchestrator session. Resolves the `main` role from settings.kdl, takes its `prompt` as `--append-system-prompt`, and execs `claude` with the actor channel enabled. Trailing arguments are forwarded to the agent CLI verbatim.

The built-in `main` role ships as the main actor. Override it with a `role "main" { ... }` block in settings.kdl to swap in your own prompt or agent. Today only `agent "claude"` is supported on this command; other agents fail with a clear error.

```bash
actor main                                # open an interactive orchestrator session
actor main "kick off the refactor"        # one-shot
actor main --model opus                   # forward flags to claude
```

## actor new

Create a new actor. If a prompt is given (positional or piped via stdin), the actor is also run immediately after creation.

| Flag | Description |
| --- | --- |
| `name` | Actor name. Becomes the git branch and worktree directory name. |
| `prompt` | Optional task prompt. If omitted and stdin is piped, stdin is read. |
| `--dir PATH` | Base directory for the worktree. Defaults to the current working directory. |
| `--no-worktree` | Skip worktree creation; run in the directory directly. |
| `--base BRANCH` | Branch to create the worktree from. Defaults to the current branch. |
| `--agent NAME` | Coding agent (`claude` or `codex`). Defaults to the role's agent or `claude`. |
| `--role NAME` | Apply a role from settings.kdl. See `actor roles` for available names. |
| `--model NAME` | Shorthand for `--config model=<name>`. |
| `--use-subscription` / `--no-use-subscription` | Force or disable subscription auth. Strips the agent's API key env var when on. |
| `--config KEY=VALUE` | Agent-arg override. Repeatable. Saved as the actor's default. |

```bash
actor new my-feature                                      # create worktree, no run
actor new my-feature "fix the nav bar"                    # create and run
actor new my-feature --role qa                            # apply a saved role
actor new my-feature --no-use-subscription                # pass API keys through
actor new my-feature --no-worktree                        # use current directory
actor new my-feature --base develop                       # branch off develop
actor new my-feature --config effort=max --model opus     # set agent config at creation
echo "fix it" | actor new my-feature                      # piped prompt
```

`--config` keys collide-check against the agent's `ACTOR_DEFAULTS` whitelist; passing an actor-key (e.g. `use-subscription`) through `--config` is rejected with a hint pointing at the dedicated flag.

## actor run

Run an existing actor with a prompt.

| Flag | Description |
| --- | --- |
| `name` | Actor name. |
| `prompt` | The task. If omitted and stdin is piped, stdin is read. Required unless `-i`. |
| `-i`, `--interactive` | Resume the actor in interactive mode (TTY passthrough). |
| `--config KEY=VALUE` | Per-run override only — not saved. Repeatable. |

```bash
actor run fix-nav "continue fixing"                       # one-shot
actor run fix-nav --config model=opus "one-off"           # temporary config
actor run fix-nav -i                                      # resume interactively
echo "fix it" | actor run fix-nav                         # piped prompt
```

The CLI prints the agent's response to stdout and propagates a non-zero exit code if the run failed.

## actor list

List actors. Optional status filter.

| Flag | Description |
| --- | --- |
| `--status STATUS` | Filter by status (`running`, `done`, `error`, etc.). |

```bash
actor list                                                # all actors
actor list --status running                               # only running
```

## actor roles

List roles defined in settings.kdl (built-in `main` plus user-level and project-level roles).

```bash
actor roles
```

## actor show

Show full details for an actor, including recent runs.

| Flag | Description |
| --- | --- |
| `name` | Actor name. |
| `--runs N` | Number of recent runs to display. Default `5`. Pass `0` to omit. |

```bash
actor show my-feature
actor show my-feature --runs 20
actor show my-feature --runs 0
```

## actor logs

View agent session output.

| Flag | Description |
| --- | --- |
| `name` | Actor name. |
| `-v`, `--verbose` | Include tool calls, thinking, and timestamps. |
| `--watch` | Stream output live as it's written. |

```bash
actor logs my-feature
actor logs my-feature --verbose
actor logs my-feature --watch
```

## actor stop

Kill the running agent for an actor. The actor row stays; only the live process is terminated.

```bash
actor stop my-feature
```

## actor config

View or update an actor's saved config. With no pairs, prints the current config; with one or more `KEY=VALUE` pairs, updates those keys. Updates take effect on the next run.

```bash
actor config my-feature                                   # view
actor config my-feature model=opus                        # update one key
actor config my-feature model=sonnet effort=max           # update several
```

## actor mcp

Start the MCP server over stdio. This is the entrypoint Claude Code uses to spawn the actor MCP — you typically don't run it by hand.

| Flag | Description |
| --- | --- |
| `--for HOST` | Coding-agent host this server is serving (e.g. `claude-code`). |

```bash
actor mcp --for claude-code
```

## actor watch

Open the dashboard TUI. Animated splash by default; use `--no-animation` over slow links.

| Flag | Description |
| --- | --- |
| `--serve` | Serve in a browser via textual-serve on port 2204. |
| `--no-animation` | Disable splash animation. |

```bash
actor watch                                               # local TUI
actor watch --serve                                       # browser-served on :2204
actor watch --no-animation                                # skip splash
```

## actor discard

Remove an actor from the database. The worktree directory stays on disk and the underlying git branch is left in place — recover the name later by running `git branch -D <name>` in the source repo if needed.

| Flag | Description |
| --- | --- |
| `name` | Actor name. |
| `-f`, `--force` | Bypass `on-discard` hook failure. |

```bash
actor discard my-feature
actor discard my-feature --force
```

## actor setup

Install (or reinstall) an integration. Idempotent — safe to re-run.

| Flag | Description |
| --- | --- |
| `--for HOST` | Required. Currently `claude-code` (skill + MCP) or `omarchy` (theme-set hook). |
| `--scope SCOPE` | `user` (default), `project`, or `local`. Ignored for `--for omarchy`. |
| `--name NAME` | MCP registration name. Default `actor`. Ignored for `--for omarchy`. |
| `--uninstall` | Remove a previously installed integration (currently only supported for `--for omarchy`). |

```bash
actor setup --for claude-code                             # user-wide install
actor setup --for claude-code --scope project             # project-local
actor setup --for claude-code --name actor-dev            # alternate MCP name
actor setup --for omarchy                                 # theme-set hook
actor setup --for omarchy --uninstall                     # remove the hook
```

## actor update

Refresh deployed skill files after upgrading actor-sh. Use `setup` for a fresh install; `update` is a lightweight refresh that keeps existing registration in place.

| Flag | Description |
| --- | --- |
| `--for HOST` | Coding-agent host. Default `claude-code`. |
| `--scope SCOPE` | Which install to refresh. Default `user`. |
| `--name NAME` | MCP name used at setup time. Default `actor`. |

```bash
actor update                                              # refresh user-wide install
actor update --scope project                              # refresh project-local
```

## actor --version

Print the installed actor-sh version.

```bash
actor --version
actor -V
```

## See also

- [MCP tool reference](../02-mcp-tools/) — the surface the orchestrator sees.
- [Config keys](../03-config-keys/) — every key per agent.
- [Roles](../../concepts/02-roles/) — how roles wire into `actor new` and `actor main`.
