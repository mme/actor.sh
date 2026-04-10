# Stage 1: Daemon + MCP Proxy

## Goal

Get a working daemon + stdio proxy that Claude Code can spawn and call tools on. Validate the full loop: Claude Code → `actor mcp` (proxy) → daemon → `commands.py` → response. No channels, no dashboard.

## Steps

### 1. Add `mcp` dependency

Add `mcp` to `[project.dependencies]` in `pyproject.toml`. Run `uv sync`.

### 2. Create the daemon

Create `src/actor/daemon.py` — a long-running process that:
- Listens on `~/.actor/actor.sock` (unix socket)
- Exposes MCP tools (start with `list_actors` only)
- Uses FastMCP `@mcp.tool` for tool definitions
- Calls existing `commands.py` functions

Subcommand: `actor daemon`

### 3. Create the MCP proxy

Create `src/actor/mcp.py` — a thin stdio-to-socket proxy that:
- Connects to `~/.actor/actor.sock`
- Auto-starts the daemon if it's not running
- Forwards MCP messages between stdio (Claude Code) and the daemon

Subcommand: `actor mcp`

### 4. Wire up CLI subcommands

Add `mcp` and `daemon` subcommands to `cli.py`.

### 5. Register with Claude Code

Create `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "actor": { "command": "actor", "args": ["mcp"] }
  }
}
```

### 6. Smoke test

Restart Claude Code. Call `list_actors` from a conversation. Verify:
- `actor mcp` starts the daemon automatically
- The tool returns actor list from the DB
- Daemon stays running after the session ends

### 7. Add remaining tools incrementally

One at a time, test each before moving on:

- `list_actors`
- `run_actor`
- `show_actor`
- `logs_actor`
- `stop_actor`
- `discard_actor`
- `pr_actor`
- `config_actor`

Each tool calls existing functions in `commands.py`. Fast iteration — editable install means code changes in the daemon are live after daemon restart.

## What's NOT in Stage 1

- Channels / push notifications
- Plugin packaging
- Status line
- Web dashboard
- Custom agent (`actor-sh.md`)
- Doctor command
