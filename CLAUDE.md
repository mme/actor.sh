# actor.sh

Parallel coding agent orchestrator. Manages multiple Claude/Codex agents running in isolated git worktrees.

## Project structure

```
skills/actor/
  actor.sh          # Shell entry point — sets PYTHONPATH and runs `python3 -m actor`
  SKILL.md          # Claude Code skill definition (the agent-facing docs)
  claude-config.md  # Claude agent config reference
  codex-config.md   # Codex agent config reference
  test_actor.py     # All tests (unittest)
  actor/            # Python package
    cli.py          # argparse CLI, command dispatch
    commands.py     # Command implementations (cmd_new, cmd_run, cmd_list, etc.)
    db.py           # SQLite database layer (~/.actor/actor.db)
    types.py        # Dataclasses: Actor, Run, Status, Config
    interfaces.py   # ABCs: Agent, GitOps, ProcessManager
    errors.py       # Exception hierarchy
    git.py          # Real git operations
    process.py      # Real process manager (kill, is_alive)
    agents/
      claude.py     # ClaudeAgent — spawns claude CLI sessions
      codex.py      # CodexAgent — spawns codex CLI sessions
```

## Development setup

### Symlink the skill for global use

The skill directory is symlinked into `~/.claude/skills/` so Claude Code can use it:

```bash
ln -s /Users/mme/Projects/actor.sh/main/skills/actor ~/.claude/skills/actor
```

Edits to files under `skills/actor/` take effect immediately — no install step.

### Install the `actor` CLI globally

For running `actor` directly from the terminal (outside Claude Code):

```bash
pip install -e .
```

This installs the `actor` console script pointing at `actor.cli:main`. The `actor.sh` wrapper is what Claude Code uses (via the skill); `pip install -e .` is for human use.

## Running tests

```bash
cd skills/actor
python -m unittest test_actor
```

Tests use in-memory SQLite and fake implementations (FakeAgent, FakeGit, FakeProcessManager) — no real processes or git repos needed.

## Key runtime paths

- **Database:** `~/.actor/actor.db` (SQLite, auto-created)
- **Worktrees:** `~/.actor/worktrees/<actor-name>/`
- **Claude logs:** `~/.claude/projects/<encoded-dir>/<session-id>.jsonl`

## Architecture notes

- Commands are pure functions that take a `Database` + interfaces and return strings. Side effects go through the `Agent`, `GitOps`, and `ProcessManager` ABCs — this is what makes everything testable with fakes.
- `actor.sh` bootstraps by setting `PYTHONPATH` to the skill directory and running `python3 -m actor`. No dependencies beyond Python 3.9+ stdlib.
- Actors spawned by other actors are tracked via the `parent` column. The `ACTOR_NAME` env var is set before launching an agent, so child actors automatically record their parent. `discard` cascades recursively — stops running children, then deletes.
- DB migrations run on open (see `db.py` after schema creation). New columns are added via `ALTER TABLE` if missing.
