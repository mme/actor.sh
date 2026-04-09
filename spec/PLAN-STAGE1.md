# Stage 1: Minimal MCP Server

## Goal

Get a working MCP server that Claude Code can spawn and call tools on. No channels, no dashboard — just MCP tools wrapping the existing `commands.py` logic.

## Steps

### 1. Create the MCP server entry point

Create `skills/actor/actor/server.py` — a minimal Python MCP server using `modelcontextprotocol/python-sdk` over stdio. Start with a single tool (`list_actors`) to validate the plumbing.

### 2. Add console script

Add `actor-mcp` to `[project.scripts]` in `pyproject.toml`:

```toml
[project.scripts]
actor = "actor.cli:main"
actor-mcp = "actor.server:main"
```

Re-run `pip install -e .` to register the new entry point.

### 3. Add `mcp` dependency

Add `mcp` to `[project.dependencies]` in `pyproject.toml`.

### 4. Smoke test

```bash
echo '{}' | actor-mcp
```

Verify the server starts and speaks MCP over stdio.

### 5. Register with Claude Code

Create `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "actor": { "command": "actor-mcp" }
  }
}
```

Restart Claude Code. It spawns the MCP server. Call `list_actors` from a conversation to verify.

### 6. Add remaining tools incrementally

One at a time, test each before moving on:

- `list_actors`
- `run_actor`
- `show_actor`
- `logs_actor`
- `stop_actor`
- `discard_actor`
- `pr_actor`
- `config_actor`

Each tool calls existing functions in `commands.py`. Fast iteration — editable pip install means code changes are live on MCP server restart (`/mcp` restart in Claude Code).

## What's NOT in Stage 1

- Channels / push notifications
- Plugin packaging
- Status line
- Web dashboard
- Custom agent (`actor-sh.md`)
- Doctor command
