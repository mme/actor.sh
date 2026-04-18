# actor.sh

Manages multiple Claude/Codex agents running in isolated git worktrees.

## Project structure

```
src/actor/               # Python package
  cli.py                 # argparse CLI, command dispatch
  commands.py            # Command implementations (cmd_new, cmd_run, cmd_list, etc.)
  setup.py               # 'actor setup' / 'actor update' — deploy bundled skill + register MCP
  server.py              # MCP server entry point
  db.py                  # SQLite database layer (~/.actor/actor.db)
  types.py               # Dataclasses: Actor, Run, Status, Config
  interfaces.py          # ABCs: Agent, GitOps, ProcessManager
  errors.py              # Exception hierarchy
  git.py                 # Real git operations
  process.py             # Real process manager (kill, is_alive)
  agents/
    claude.py            # ClaudeAgent — spawns claude CLI sessions
    codex.py             # CodexAgent — spawns codex CLI sessions
  _skill/                # Bundled Claude Code skill (agent-facing docs)
    SKILL.md             # Main skill definition
    cli.md               # CLI fallback reference
    claude-config.md     # Claude agent config reference
    codex-config.md      # Codex agent config reference
.claude-plugin/
  plugin.json            # Declares src/actor/_skill as a skill location for
                         # tooling like npx skills
tests/
  test_*.py              # unittest suites
spec/
  V2.md                  # V2 vision (MCP server, channels, dashboard, plugin)
  PLAN-STAGE1.md         # Stage 1 implementation plan (minimal MCP server)
```

## Development setup

### Install with uv

```bash
uv sync                    # creates .venv, installs in editable mode
uv tool install -e .       # makes `actor` globally available
```

Changes to `src/actor/` take effect immediately — no reinstall needed.
Re-run `uv tool install -e .` only when adding new console scripts to `[project.scripts]`.

### Register the skill + MCP with Claude Code

```bash
actor setup --for claude-code        # user-wide (writes ~/.claude/skills/actor + registers MCP)
actor setup --for claude-code --scope project   # project-local (./.claude/skills/actor + ./.mcp.json)
```

For dev work, after editing `src/actor/_skill/*.md` re-run:

```bash
actor update                         # refreshes the deployed skill files in place
```

## Running tests

```bash
uv run python -m unittest tests.test_actor
```

Tests use in-memory SQLite and fake implementations (FakeAgent, FakeGit, FakeProcessManager) — no real processes or git repos needed.

## Key runtime paths

- **Database:** `~/.actor/actor.db` (SQLite, auto-created)
- **Worktrees:** `~/.actor/worktrees/<actor-name>/`
- **Claude logs:** `~/.claude/projects/<encoded-dir>/<session-id>.jsonl`

## Architecture notes

- Commands are pure functions that take a `Database` + interfaces and return strings. Side effects go through the `Agent`, `GitOps`, and `ProcessManager` ABCs — this is what makes everything testable with fakes.
- No dependencies beyond Python 3.9+ stdlib for the core package.
- Actors spawned by other actors are tracked via the `parent` column. The `ACTOR_NAME` env var is set before launching an agent, so child actors automatically record their parent. `discard` cascades recursively — stops running children, then deletes.
- DB migrations run on open (see `db.py` after schema creation). New columns are added via `ALTER TABLE` if missing.
