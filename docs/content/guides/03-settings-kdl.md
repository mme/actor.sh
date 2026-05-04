---
title: "settings.kdl Tour"
description: "Roles, per-agent defaults, hooks, and ask blocks — every top-level node explained."
weight: 3
---

`settings.kdl` is where you teach actor.sh about the roles, defaults, hooks, and orchestrator guidance specific to your environment. This tour walks through every top-level block and explains how the user-wide and project-local files combine.

## File locations

Two paths are read, in order:

- `~/.actor/settings.kdl` — user-wide.
- `<repo>/.actor/settings.kdl` — project-local. Discovered by walking up from your current working directory; the closest one wins.

Project values override user values per-key. Missing files are skipped silently. There is no `actor init` — create the file by hand. The `.actor/` directory typically already exists since actor.sh uses it for worktrees and the SQLite database.

Load the merged config programmatically with `actor.config.load_config(cwd=..., home=...)`; both arguments default to `Path.cwd()` and `$HOME` so tests can inject temp dirs.

## role "<name>" { ... }

A role is a named preset for `actor new`. See the [Roles](../../concepts/02-roles/) concept page for the full story; the short version is that a role bundles an agent kind, a system prompt that shapes the actor's identity, and any number of config keys.

```kdl
role "reviewer" {
    description "Concise code review; flag bugs and style issues."
    agent "claude"
    model "opus"
    prompt "You are a senior code reviewer. Be concise; flag bugs, security issues, and style violations. Don't fix anything — report findings only."
}

role "qa" {
    description "Run the test suite and triage failures."
    agent "claude"
    permission-mode "acceptEdits"
    prompt "You are a QA engineer. Run the test suite, triage failures, and report root causes."
}

role "designer" {
    description "Frontend / UX work."
    agent "claude"
    model "opus"
    allowed-tools "Read Edit Grep Glob Bash"
    prompt "You are a senior frontend engineer. Prioritize accessibility and visual polish."
}
```

`agent`, `prompt`, and `description` are promoted to top-level fields on the role. Every other child becomes a config key for the actor.

A built-in `main` role exists by default — it's the main actor preset used by `actor main`. Override it by adding your own `role "main" { ... }` block; the override replaces the built-in wholesale (no per-field merge for roles).

## defaults "<agent>" { ... }

Per-agent defaults that apply to every actor of that kind. There's one block per agent — `defaults "claude" { ... }` and `defaults "codex" { ... }`.

```kdl
defaults "claude" {
    use-subscription true
    permission-mode "acceptEdits"
    model "opus"
}

defaults "codex" {
    m "o3"
    sandbox "workspace-write"
}
```

All keys live in one flat namespace. Each key is routed at parse time by checking the agent class's `ACTOR_DEFAULTS` whitelist:

- **Whitelisted keys** (e.g. `use-subscription`) are actor-sh interpreted and never forwarded to the agent binary.
- **Everything else** becomes a CLI flag on the agent binary. Claude uses semantic long flags (`permission-mode "auto"` → `--permission-mode auto`); Codex uses native flag names (`m "o3"` → `-m o3`, `sandbox "workspace-write"` → `--sandbox workspace-write`).

A `null` value cancels a lower-precedence default:

```kdl
defaults "claude" {
    permission-mode null   # drop the built-in "auto" default
}
```

### Merge precedence

At `actor new` time, lowest to highest:

1. Class defaults baked into the agent (`AGENT_DEFAULTS` + `ACTOR_DEFAULTS`).
2. User `~/.actor/settings.kdl` `defaults` block.
3. Project `<repo>/.actor/settings.kdl` `defaults` block.
4. The role chosen with `--role`.
5. Per-call CLI `--config key=value`.

The resolved merge is **snapshotted into the database at creation**. Later edits to settings.kdl don't retroactively change existing actors — use `actor config <name> key=value` to mutate an actor's stored config. At `actor run` time the stored config is the base and per-run `--config` arguments layer on top for that single run only. `null` at a higher layer cancels lower defaults; the emitter drops keys whose final value is `None`.

For per-agent details, see [Configuring the Claude Agent](../01-claude-config/) and [Configuring the Codex Agent](../02-codex-config/).

### Built-in class defaults

Without any settings.kdl, you inherit:

- **Claude** — `use-subscription "true"`, `permission-mode "auto"`.
- **Codex** — `use-subscription "true"`, `sandbox "danger-full-access"`, `a "never"`.

## hooks { ... }

Shell commands that fire around lifecycle events. See [Lifecycle hooks](../../concepts/03-hooks/) for the full event reference.

```kdl
hooks {
    on-start   "kubectl config use-context dev"
    before-run "git fetch --quiet"
    after-run  "./scripts/notify.sh"
    on-discard "git diff --quiet && git diff --quiet --staged"
}
```

Each value is run via `/bin/sh -c` in the actor's worktree, with `ACTOR_NAME`, `ACTOR_DIR`, `ACTOR_AGENT`, and (when set) `ACTOR_SESSION_ID` exported into the environment.

## ask { ... }

Free-form natural-language guidance appended to the orchestrator's MCP tool descriptions at server startup. See [Ask blocks](../../concepts/04-ask-blocks/) for the full key reference.

```kdl
ask {
    on-start   "Always confirm the agent kind for risky tasks."
    before-run "Skip questions; assume per-run config never changes."
    on-discard null   # silence the default — discard never asks
}
```

Valid keys are `on-start` (appended to `new_actor`), `before-run` (appended to `run_actor`), and `on-discard` (appended to `discard_actor`). A `null` or empty-string value silences the hardcoded default for that key.

Tool descriptions are static for the lifetime of the MCP server, so edits to the `ask` block apply on the next `actor main` invocation.

## Forward compatibility

Unknown top-level nodes — for example, `alias` — are silently ignored at parse time. This keeps existing settings.kdl files compatible with future blocks introduced by follow-up tickets.

## When KDL is malformed

If the parser cannot read a settings file, actor.sh raises `ConfigError` with the offending path included so you can find and fix the syntax. Missing files are not errors; only malformed ones halt the run.
