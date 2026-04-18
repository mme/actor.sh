# actor.sh

Manages multiple Claude/Codex agents running in isolated git worktrees.

## Setup

```bash
# Install uv (if needed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install actor-sh
uv tool install actor-sh            # or: pip install actor-sh

# Register the Claude Code skill + MCP server
actor setup --for claude-code       # user-wide
# or: actor setup --for claude-code --scope project   # project-local

# Verify
actor --help
```

After bumping `actor-sh` to a new version, refresh the deployed skill:

```bash
actor update
```

## Running tests

```bash
uv run python -m unittest tests.test_actor
```
