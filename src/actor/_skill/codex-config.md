# Codex Agent Configuration

All options below are set via `--config key=value` on `actor new` or `actor config`. Most are passed straight through to `codex` as CLI flags on every run (one-character keys like `m`, `a` become short flags `-m`, `-a`; longer keys become `--key value`); a few (listed as "actor-sh interpreted") are consumed by actor-sh itself and never reach the agent binary.

Codex uses its native flag names verbatim — the key you write in config is the flag name Codex receives. This is different from the Claude agent's semantic long-flag naming.

## Use Subscription

Actor-sh interpreted. When `true` (the default), actor-sh strips `OPENAI_API_KEY` from the agent's environment so Codex uses the logged-in `codex` subscription. Set to `false` to keep the API key and bill requests against it.

```
actor new my-feature --agent codex --config use-subscription=false
```

## Model (`m`)

Select which model to use. Can also be set via `--model` on `actor new`.

```
actor new my-feature --agent codex --config m=o3
```

Default: agent's default model.

## Sandbox (`sandbox`)

Controls the sandbox policy for shell commands.

```
actor new my-feature --agent codex --config sandbox=workspace-write
```

Options:
- `danger-full-access` (default) — no filesystem sandboxing
- `workspace-write` — allow writes only in the workspace
- `read-only` — no writes allowed

## Approval (`a`)

Controls when the agent asks for approval before running commands.

```
actor new my-feature --agent codex --config a=on-request
```

Options:
- `never` (default) — never ask, execute everything
- `on-request` — agent decides when to ask
- `untrusted` — only auto-approve trusted commands (ls, cat, etc.)

## Additional Directories

Grant the agent write access to directories outside the worktree.

```
actor new my-feature --agent codex --config add-dir=/path/to/shared/lib
```

## Web Search

Enable live web search for the agent.

```
actor new my-feature --agent codex --config search=true
```
