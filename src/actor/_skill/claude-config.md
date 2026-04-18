# Claude Agent Configuration

All options below are set via `--config key=value` on `actor new` or `actor config`. They are passed to `claude` on every run.

## Model

Select which Claude model to use. Can also be set via `--model` on `actor new`.

```
actor new my-feature --config model=sonnet
```

Options: `sonnet`, `opus`, `haiku`, or a full model ID (e.g., `claude-sonnet-4-6`). Default: agent's default model.

## Permission Mode

Controls how the agent handles permission checks.

```
actor new my-feature --config permission-mode=auto
```

Options:
- `bypassPermissions` (default) — skip all permission checks, fully autonomous
- `auto` — agent decides when to ask for approval
- `acceptEdits` — auto-approve file edits, ask for other actions
- `default` — standard permission prompts
- `dontAsk` — never ask, skip actions that need approval
- `plan` — plan mode, no edits

## Effort

Controls the thinking effort level.

```
actor new my-feature --config effort=max
```

Options: `low`, `medium`, `high` (default), `max`.

## System Prompt

Replace the default system prompt entirely. Use this when you want to create a specialized agent with a specific role or expertise — for example, a security auditor, a documentation writer, or a domain expert.

```
actor new my-reviewer --config system-prompt="You are a senior security engineer. Review all code for vulnerabilities."
```

## Append System Prompt

Add instructions on top of the default system prompt. Use this when you want to guide the agent's behavior without replacing its base capabilities.

```
actor new my-feature --config append-system-prompt="Always write tests for new code. Use pytest."
```

## Allowed Tools / Disallowed Tools

Restrict which tools the agent can use. Useful for limiting scope — for example, preventing an agent from running shell commands.

```
actor new my-feature --config allowed-tools="Read Edit Grep Glob"
actor new my-feature --config disallowed-tools="Bash"
```

## Additional Directories

Grant the agent access to directories outside the worktree.

```
actor new my-feature --config add-dir=/path/to/shared/lib
```

## MCP Config

Load MCP servers from a JSON config file, giving the agent access to external tools.

```
actor new my-feature --config mcp-config=/path/to/mcp.json
```
