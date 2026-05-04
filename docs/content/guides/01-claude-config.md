---
title: "Configuring the Claude Agent"
description: "Every config key the Claude agent accepts, with CLI and settings.kdl examples."
weight: 1
slug: "claude-config"
---

The Claude agent is configured through key-value pairs that you pass on the CLI, define inside a [Role](../../concepts/02-roles/), or set as defaults in [settings.kdl](../03-settings-kdl/). All three paths use the same key names — what changes is how the value is layered into the final config.

This page lists every key the Claude agent recognizes and shows both the per-actor CLI form and the role / settings.kdl form for each.

## How keys are routed

Two categories of keys exist:

- **Actor-sh interpreted keys** are consumed by actor.sh itself and never reach the `claude` binary. The whitelist lives on `ClaudeAgent.ACTOR_DEFAULTS`. Today this is just `use-subscription`.
- **Everything else** is forwarded to `claude` verbatim as `--<key> <value>`. Claude uses semantic long flags, so the key you write is the flag Claude receives. Unknown keys are passed through and `claude` decides whether they're valid.

Set keys per-actor with `--config`:

```bash
actor new my-feature --config model=opus --config permission-mode=acceptEdits "Refactor auth module"
```

Or as defaults in `~/.actor/settings.kdl`:

```kdl
defaults "claude" {
    model "opus"
    permission-mode "acceptEdits"
}
```

Or as part of a role:

```kdl
role "reviewer" {
    agent "claude"
    model "opus"
    permission-mode "plan"
    prompt "You are a senior code reviewer. Report findings only."
}
```

The merge order, lowest to highest, is: class defaults → user kdl → project kdl → role → CLI `--config`. See [settings.kdl](../03-settings-kdl/) for the full breakdown.

## use-subscription

Actor-sh interpreted. When `true` (the default), actor.sh strips `ANTHROPIC_API_KEY` from the agent's environment so Claude uses your logged-in `claude` subscription. Set to `false` to keep the API key and bill against it instead.

```bash
actor new my-feature --config use-subscription=false
```

```kdl
defaults "claude" {
    use-subscription false
}
```

## model

Selects which Claude model to use. You can also set this with the dedicated `--model` flag on `actor new`, which is equivalent to `--config model=<value>`.

```bash
actor new my-feature --model opus
actor new my-feature --config model=sonnet
```

Accepts `sonnet`, `opus`, `haiku`, or a full model ID like `claude-sonnet-4-6`. Default: Claude's own default model.

## permission-mode

Controls how the agent handles permission checks. Default: `auto`, which in an isolated actor worktree is effectively autonomous.

```bash
actor new my-feature --config permission-mode=acceptEdits
```

Options:

- `auto` (default) — agent decides when to ask for approval
- `bypassPermissions` — skip all permission checks
- `acceptEdits` — auto-approve file edits, ask for other actions
- `default` — standard permission prompts
- `dontAsk` — never ask, skip actions that need approval
- `plan` — plan mode, no edits

## effort

Controls the thinking effort level. Options: `low`, `medium`, `high`, `max`. Default: `high`.

```bash
actor new my-feature --config effort=max
```

## system-prompt

Replaces Claude Code's default system prompt entirely. Use this for highly specialized agents where you don't want Claude Code's standard tooling guidance.

```bash
actor new auditor --config system-prompt="You are a senior security engineer. Review all code for vulnerabilities."
```

## append-system-prompt

Layers extra instructions on top of the default system prompt. This is the same key roles use under the hood — a role's `prompt` field is injected as `--append-system-prompt`, so role identity stacks with Claude Code's standard behavior.

```bash
actor new my-feature --config append-system-prompt="Always write tests. Use pytest."
```

## allowed-tools / disallowed-tools

Restrict which tools the agent can use. Values are a single string of tool names; quote it on the shell.

```bash
actor new my-feature --config allowed-tools="Read Edit Grep Glob"
actor new my-feature --config disallowed-tools="Bash"
```

## add-dir

Grants the agent access to directories outside its worktree.

```bash
actor new my-feature --config add-dir=/path/to/shared/lib
```

## mcp-config

Loads MCP servers from a JSON config file, giving the actor access to external tools.

```bash
actor new my-feature --config mcp-config=/path/to/mcp.json
```

## Built-in defaults

Even without a settings.kdl, every Claude actor starts with:

```kdl
defaults "claude" {
    use-subscription "true"
    permission-mode "auto"
}
```

These come from `ClaudeAgent.AGENT_DEFAULTS` and `ClaudeAgent.ACTOR_DEFAULTS`. Cancel a built-in by setting it to `null` in your kdl:

```kdl
defaults "claude" {
    permission-mode null
}
```
