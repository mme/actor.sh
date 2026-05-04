---
title: "Config Keys"
description: "Every config key per agent — actor-keys, agent-args, defaults, and valid values."
weight: 3
slug: "config-keys"
---

Every config key actor-sh recognizes, grouped by agent. Use this page when you need to look up what a key does, what its default is, or what values it accepts.

## actor-key vs agent-arg

actor-sh routes each config key into one of two buckets at parse time:

- **actor-keys** are interpreted by actor-sh and are never forwarded to the agent binary. The whitelist is `ACTOR_DEFAULTS` on the agent class — currently just `use-subscription` for both Claude and Codex.
- **agent-args** are forwarded to the agent CLI as flags. Claude uses semantic long flags: `permission-mode "auto"` becomes `--permission-mode auto`. Codex uses native flag names verbatim — one-character keys become short flags (`m "o3"` → `-m o3`), longer keys become long flags (`sandbox "workspace-write"` → `--sandbox workspace-write`).

Anything not in the actor-key whitelist is treated as an agent-arg and forwarded to the agent binary. The agent decides whether the flag is valid; unknown keys can fail at run time.

A `null` value in a kdl `defaults` block cancels a lower-precedence default — useful for dropping a built-in default without replacing it. Empty-string values emit a bare flag with no argument.

## Setting config

Three entry points cover every workflow. They share validation logic; an actor-key passed through `--config` (or MCP `config=[...]`) is rejected with a hint pointing at the dedicated entrypoint.

```bash
actor new my-feature --config model=opus --config effort=max
actor config my-feature model=sonnet
```

```kdl
defaults "claude" {
    permission-mode "auto"
    model "opus"
}
```

Merge precedence at actor creation, lowest to highest: class `AGENT_DEFAULTS` + `ACTOR_DEFAULTS` → user kdl `defaults` block → project kdl `defaults` block → role config → CLI `--config`. The resolved merge is snapshotted into the DB; later edits to settings.kdl don't retroactively change existing actors. At run time the stored config is the base and per-run `--config` layers on top for that one run.

## Claude

| Key | Kind | Default | Valid values | Description |
| --- | --- | --- | --- | --- |
| `use-subscription` | actor-key | `true` | `true`, `false` | When `true`, strips `ANTHROPIC_API_KEY` from the agent's env so Claude uses the logged-in subscription. Set to `false` to bill against the API key. |
| `permission-mode` | agent-arg | `auto` | `auto`, `bypassPermissions`, `acceptEdits`, `default`, `dontAsk`, `plan` | How the agent handles permission checks. `auto` is effectively autonomous inside a worktree. |
| `model` | agent-arg | (agent's default) | `sonnet`, `opus`, `haiku`, or a full model ID | Which Claude model to use. Also settable via `--model` on `actor new`. |
| `effort` | agent-arg | (none) | `low`, `medium`, `high`, `max` | Thinking effort level. actor.sh sets no default; the agent's own default applies if unset. |
| `system-prompt` | agent-arg | (none) | string | Replace the default system prompt entirely. |
| `append-system-prompt` | agent-arg | (none) | string | Add instructions on top of the default system prompt. This is also where roles inject their `prompt`. |
| `allowed-tools` | agent-arg | (none) | space-separated tool names | Whitelist the tools the agent may use, e.g. `"Read Edit Grep Glob"`. |
| `disallowed-tools` | agent-arg | (none) | space-separated tool names | Blacklist tools, e.g. `"Bash"`. |
| `add-dir` | agent-arg | (none) | path | Grant the agent access to a directory outside the worktree. |
| `mcp-config` | agent-arg | (none) | path to JSON | Load MCP servers from a JSON config, giving the agent access to external tools. |

The class-level baseline is `AGENT_DEFAULTS = {"permission-mode": "auto"}` and `ACTOR_DEFAULTS = {"use-subscription": "true"}`. The role's `prompt` field is injected as `--append-system-prompt` (the `SYSTEM_PROMPT_KEY` for Claude).

```kdl
defaults "claude" {
    use-subscription true
    permission-mode "auto"
    model "opus"
}
```

## Codex

| Key | Kind | Default | Valid values | Description |
| --- | --- | --- | --- | --- |
| `use-subscription` | actor-key | `true` | `true`, `false` | When `true`, strips `OPENAI_API_KEY` from the agent's env so Codex uses the logged-in subscription. Set to `false` to bill against the API key. |
| `sandbox` | agent-arg | `danger-full-access` | `danger-full-access`, `workspace-write`, `read-only` | Sandbox policy for shell commands. `danger-full-access` is unsandboxed; `workspace-write` allows writes only inside the workspace; `read-only` blocks all writes. |
| `a` | agent-arg | `never` | `never`, `on-request`, `untrusted` | Approval policy. `never` auto-approves everything; `on-request` lets the agent decide when to ask; `untrusted` only auto-approves a small set of trusted commands. |
| `m` | agent-arg | (agent's default) | model name | Codex model. Also settable via `--model` on `actor new`. |
| `add-dir` | agent-arg | (none) | path | Grant the agent write access outside the worktree. |
| `search` | agent-arg | (none) | `true` | Enable live web search. |

The class-level baseline is `AGENT_DEFAULTS = {"sandbox": "danger-full-access", "a": "never"}` and `ACTOR_DEFAULTS = {"use-subscription": "true"}`. Codex has no first-class system-prompt CLI flag, so `SYSTEM_PROMPT_KEY` is `None` and roles with a `prompt` field combined with `agent "codex"` are rejected at `actor new` time.

```kdl
defaults "codex" {
    use-subscription true
    m "o3"
    sandbox "workspace-write"
    a "on-request"
}
```

## Cancelling a default

A higher-precedence layer can drop a key with `null`:

```kdl
defaults "claude" {
    permission-mode null   # drop the built-in "auto" default
}
```

The emitter skips keys whose final resolved value is `None`, so the flag never reaches the agent binary.

## See also

- [Claude agent config guide](../../guides/01-claude-config/) — fuller examples and notes per key.
- [Codex agent config guide](../../guides/02-codex-config/) — same, for Codex.
- [CLI reference](../01-cli/) — `actor new`, `actor config`, and `actor run` flag surfaces.
- [MCP tool reference](../02-mcp-tools/) — `config=[...]` argument shape on the MCP side.
