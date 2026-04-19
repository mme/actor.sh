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
actor setup --for claude-code                    # user-wide
actor setup --for claude-code --scope project    # project-local
```

Launch a session that has the actor channel enabled:

```bash
actor claude                                      # wraps `claude --dangerously-load-development-channels server:actor`
```

Sub-claudes spawned by actors inherit the same flag automatically (see
`ClaudeAgent._CHANNEL_ARGS`), so nested actors can receive completion
notifications too.

For dev work, after editing `src/actor/_skill/*.md`:

```bash
actor update                                      # refreshes deployed skill files in place
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

## Releasing

The package uses dynamic versioning via `hatchling` + `hatch-vcs` — the version
is derived from the latest git tag at build time. There is no hardcoded
`version = "..."` in `pyproject.toml`.

`.github/workflows/release.yml` runs on every push to `main` that touches
non-doc/non-CI paths:

1. Run unit tests.
2. Read the latest `v*` tag, bump the patch (or whatever `workflow_dispatch`
   input `bump` requests), compute the new tag `vX.Y.Z`.
3. Create and push the tag — no commit back to `main`.
4. `uv build` — hatch-vcs stamps the wheel with `X.Y.Z`.
5. Publish to PyPI via trusted publishing (OIDC, no tokens).
6. Create a GitHub release with auto-generated notes + wheel attached.

Local dev installs (`uv sync`) get a PEP 440 dev version like
`0.1.4.dev3+g1a2b3c4` derived from git state at install time, so
`actor --version`, the MCP server's announced version, and the deployed
SKILL.md all agree and the drift check still works.

## Architecture notes

- Commands are pure functions that take a `Database` + interfaces and return strings. Side effects go through the `Agent`, `GitOps`, and `ProcessManager` ABCs — this is what makes everything testable with fakes.
- No dependencies beyond Python 3.9+ stdlib for the core package.
- Actors spawned by other actors are tracked via the `parent` column. The `ACTOR_NAME` env var is set before launching an agent, so child actors automatically record their parent. `discard` cascades recursively — stops running children, then deletes.
- DB migrations run on open (see `db.py` after schema creation). New columns are added via `ALTER TABLE` if missing.
