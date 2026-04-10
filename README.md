# actor.sh

Fork reality, create in parallel. Manages multiple Claude/Codex agents running in isolated git worktrees.

## Setup

```bash
# Install uv (if needed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/mme/actor.sh.git
cd actor.sh
uv sync
uv tool install -e .

# Symlink the skill for Claude Code
ln -s "$(pwd)/skills/actor" ~/.claude/skills/actor

# Verify
actor --help
```

## MCP server (channels)

```bash
# Start Claude Code with channel support
claude --dangerously-load-development-channels server:actor
```

The `.mcp.json` in the repo root configures the MCP server automatically.

## Running tests

```bash
uv run python -m unittest tests.test_actor
```
