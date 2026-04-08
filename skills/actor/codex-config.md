# Codex Agent Configuration

All options below are set via `--config key=value` on `actor new` or `actor config`. They are passed to `codex` on every run.

## Model

Select which model to use. Can also be set via `--model` on `actor new`.

```
actor new my-feature --agent codex --config model=o3
```

Default: agent's default model.

## Sandbox

Controls the sandbox policy for shell commands.

```
actor new my-feature --agent codex --config sandbox=workspace-write
```

Options:
- `danger-full-access` (default) — no filesystem sandboxing
- `workspace-write` — allow writes only in the workspace
- `read-only` — no writes allowed

When neither `sandbox` nor `approval` is set, the default is `--dangerously-bypass-approvals-and-sandbox` (full access, no approval prompts).

## Approval

Controls when the agent asks for approval before running commands.

```
actor new my-feature --agent codex --config approval=on-request
```

Options:
- `never` (default) — never ask, execute everything
- `on-request` — agent decides when to ask
- `untrusted` — only auto-approve trusted commands (ls, cat, etc.)

When neither `sandbox` nor `approval` is set, the default is `--dangerously-bypass-approvals-and-sandbox` (full access, no approval prompts).

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
