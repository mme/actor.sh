---
title: "Roles"
description: "Named presets in settings.kdl that bundle an agent, a system prompt, and config keys."
weight: 2
---

A **role** is a named preset for `actor new`. It bundles an agent kind, a system prompt that shapes the actor's identity, and any number of config keys you want defaulted in. Roles let you define "what kind of actor" once and reuse it across tasks.

## Defining a role

Roles live in `~/.actor/settings.kdl` (user-wide) or `<repo>/.actor/settings.kdl` (project-local). Each role is a top-level `role` block with the role name as its sole positional argument:

```kdl
role "reviewer" {
    description "Concise code review; flag bugs and style issues."
    agent "claude"
    model "sonnet"
    prompt "You are a senior code reviewer. Be concise; flag bugs, security issues, and style violations. Don't fix anything — report findings only."
}

role "qa" {
    description "Run tests after changes; report failures concisely."
    agent "claude"
    model "opus"
    effort "max"
    prompt "You're a QA engineer. Run the tests, report what fails."
}
```

The recognized fields inside a role block are:

- `agent` — `"claude"` or `"codex"`. Picks which CLI the actor runs.
- `prompt` — the role's **system prompt** (its identity), described below.
- `description` — short "when to use this role" text, surfaced by `actor roles` and `list_roles`.
- Any other key (e.g. `model`, `effort`, `permission-mode`, `use-subscription`) — stored as a config key and merged into the actor's config at creation time.

Project-level roles override user-level ones with the same name (whole-role replacement, no per-field merge).

## Prompt is identity, not task

The `prompt` field is the actor's **system prompt**, not a default task. For a Claude actor, it's injected as `--append-system-prompt`, layering the role's identity on top of Claude Code's defaults. The per-call task — the prompt you pass to `actor new` or `new_actor` — is something else; it tells the actor what to do, not who it is. They coexist.

```bash
actor new auth-review --role reviewer "Review src/auth/*.py for security issues"
```

Here `reviewer`'s `prompt` (the system prompt) gives the actor its reviewer identity; `"Review src/auth/*.py..."` is the task.

Codex doesn't yet support role-level system prompts. A role with both `prompt` and `agent "codex"` is rejected at `actor new` time with a clear error.

## The built-in `main` role

A built-in `main` role exists by default — the main actor preset that `actor main` loads. Override it by adding `role "main" { ... }` to your settings.kdl; the override replaces the built-in entirely.

## Discovery

You don't have to remember role names — `actor roles` (CLI) and `mcp__actor__list_roles` (MCP) print the merged role table with names, agents, and descriptions. If you typo a role name on `actor new --role <bad>`, the error lists what's available.

## Precedence

At `actor new`, role values are layered between settings.kdl defaults and explicit CLI flags:

1. Class-level hardcoded defaults
2. User `defaults` block in settings.kdl
3. Project `defaults` block
4. Role config (`--role`)
5. Explicit CLI overrides (`--agent`, `--config key=value`)

Explicit `--agent` or `--config` flags beat the role; the per-call task prompt doesn't compete with the role's system prompt.

See the [settings.kdl tour](../../guides/03-settings-kdl/) for the full file shape and the [config keys reference](../../reference/01-config-keys/) for valid keys per agent.
