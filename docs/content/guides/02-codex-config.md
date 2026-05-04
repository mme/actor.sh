---
title: "Configuring the Codex Agent"
description: "Every config key the Codex agent accepts, with CLI and settings.kdl examples."
weight: 2
slug: "codex-config"
---

The Codex agent is configured the same way as Claude — `--config` on the CLI, inside a [Role](../../concepts/02-roles/), or as defaults in [settings.kdl](../03-settings-kdl/). The difference is that Codex uses its native flag names verbatim: there's no semantic translation layer between what you write and what `codex` receives.

This page lists every Codex config key and shows both the CLI form and the kdl form.

## How keys are routed

Two categories of keys exist:

- **Actor-sh interpreted keys** are consumed by actor.sh itself and never reach the `codex` binary. The whitelist lives on `CodexAgent.ACTOR_DEFAULTS`. Today this is just `use-subscription`.
- **Everything else** is forwarded to `codex` as a CLI flag. One-character keys become short flags (`m` → `-m`, `a` → `-a`); longer keys become long flags (`sandbox` → `--sandbox`, `add-dir` → `--add-dir`). Unknown keys are passed through and `codex` decides whether they're valid.

Set keys per-actor with `--config`:

```bash
actor new my-feature --agent codex --config m=o3 --config sandbox=workspace-write "Investigate failing tests"
```

Or as defaults in `~/.actor/settings.kdl`:

```kdl
defaults "codex" {
    m "o3"
    sandbox "workspace-write"
}
```

The full merge order, lowest to highest, is: class defaults → user kdl → project kdl → role → CLI `--config`. See [settings.kdl](../03-settings-kdl/) for the breakdown.

## use-subscription

Actor-sh interpreted. When `true` (the default), actor.sh strips `OPENAI_API_KEY` from the agent's environment so Codex uses your logged-in `codex` subscription. Set to `false` to keep the API key and bill against it.

```bash
actor new my-feature --agent codex --config use-subscription=false
```

```kdl
defaults "codex" {
    use-subscription false
}
```

## m (model)

Selects the model. The key is `m` because that's Codex's own short flag — actor.sh emits `-m <value>`. You can also set this with the dedicated `--model` flag on `actor new`, which routes to the same `m` key.

```bash
actor new my-feature --agent codex --config m=o3
actor new my-feature --agent codex --model o3
```

Default: Codex's own default model.

## sandbox

Controls the sandbox policy for shell commands. Default: `danger-full-access` — actor.sh ships this as a class-level default because actors already run in isolated worktrees.

```bash
actor new my-feature --agent codex --config sandbox=workspace-write
```

Options:

- `danger-full-access` (default) — no filesystem sandboxing
- `workspace-write` — allow writes only in the workspace
- `read-only` — no writes allowed

## a (approval)

Controls when the agent asks for approval before running commands. The key is `a` because that's Codex's short flag. Default: `never`.

```bash
actor new my-feature --agent codex --config a=on-request
```

Options:

- `never` (default) — never ask, execute everything
- `on-request` — agent decides when to ask
- `untrusted` — auto-approve only trusted commands (`ls`, `cat`, etc.)

## add-dir

Grants the agent write access to directories outside its worktree. Codex receives `--add-dir <path>`.

```bash
actor new my-feature --agent codex --config add-dir=/path/to/shared/lib
```

## search

Enables live web search. Codex receives `--search true`.

```bash
actor new my-feature --agent codex --config search=true
```

## Built-in defaults

Without any settings.kdl, every Codex actor starts with:

```kdl
defaults "codex" {
    use-subscription "true"
    sandbox "danger-full-access"
    a "never"
}
```

These come from `CodexAgent.AGENT_DEFAULTS` and `CodexAgent.ACTOR_DEFAULTS`. Cancel any of them by setting the key to `null` in your kdl.

## Codex compatibility note

actor.sh works for Codex agents driven from the CLI — `actor new`, `actor run`, `actor watch` all behave the same regardless of agent kind. However, **the orchestrator integration (Claude Code via the actor MCP server) is the recommended driver, not Codex.**

Codex does not currently forward MCP server notifications into the model's conversation, which means actors spawned from a Codex orchestrator finish silently — the model never sees the completion event. Tracked upstream in [openai/codex#17543](https://github.com/openai/codex/issues/17543) and [openai/codex#18056](https://github.com/openai/codex/issues/18056).

Until Codex ships notification forwarding, use Claude Code as the orchestrator and let it spawn Codex actors as needed.

## Roles and Codex

Roles can bundle Codex config keys exactly like Claude — `m`, `sandbox`, `a`, etc. — but they cannot supply a system prompt for a Codex actor. Codex has no `--append-system-prompt`-style flag (instructions live in `AGENTS.md` / `config.toml`), so a role with both `agent "codex"` and a `prompt` field is rejected at `actor new` time with a clear error rather than silently dropped.
