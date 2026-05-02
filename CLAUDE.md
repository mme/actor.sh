# actor.sh

Manages multiple Claude/Codex agents running in isolated git worktrees.

## Project structure

```
src/actor/               # Python package
  cli.py                 # argparse CLI, command dispatch
  commands.py            # Command implementations (cmd_new, cmd_run, cmd_list, etc.)
  config.py              # KDL loader for ~/.actor/settings.kdl + <repo>/.actor/settings.kdl — roles
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
  watch/
    app.py               # Textual dashboard
    interactive/         # Embedded terminal for live Claude/Codex sessions
      screen.py          # pyte wrapper + rich.Text rendering
      input.py           # key + mouse → ANSI byte translator
      batcher.py         # refresh coalescer (flicker prevention)
      diagnostics.py     # ring buffer of I/O events for post-mortem
      pty_session.py     # pty.fork + async read/write/resize/reap
      widget.py          # Textual widget (glue)
      manager.py         # per-actor session registry + DB integration
  _skill/                # Bundled Claude Code skill (agent-facing docs)
    SKILL.md             # Main skill definition
    cli.md               # CLI fallback reference
    claude-config.md     # Claude agent config reference
    codex-config.md      # Codex agent config reference
tests/
  test_*.py              # unittest suites
spec/
  V2.md                  # V2 vision (MCP server, channels, dashboard, plugin)
  DASHBOARD.md           # Watch dashboard spec
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
uv run python -m unittest discover tests      # full suite
uv run python -m unittest tests.test_actor    # single module
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

`.github/workflows/release.yml` is triggered manually via
`workflow_dispatch` (run it from the GitHub Actions UI or
`gh workflow run release.yml -f bump=patch|minor|major`):

1. Run unit tests.
2. Read the latest `v*` tag, bump per the `bump` input, compute `vX.Y.Z`.
3. Create and push the tag — no commit back to `main`.
4. `uv build` — hatch-vcs stamps the wheel with `X.Y.Z`.
5. Publish to PyPI via trusted publishing (OIDC, no tokens).
6. Create a GitHub release with auto-generated notes + wheel attached.

Local dev installs (`uv sync`) get a PEP 440 dev version like
`0.1.4.dev3+g1a2b3c4` derived from git state at install time, so
`actor --version`, the MCP server's announced version, and the deployed
SKILL.md all agree and the drift check still works.

## Config files & roles

`actor new` reads `~/.actor/settings.kdl` (user-wide) and
`<repo>/.actor/settings.kdl` (project-local, discovered by walking up from
CWD). Project values win when the same key appears in both. Missing files
are ignored silently; malformed KDL raises `ConfigError` with the path.

There is no `actor init` — create the file by hand (the `.actor/`
directory is also used for worktrees and the SQLite DB, so it typically
already exists).

Roles are named presets for `actor new`:

```kdl
role "qa" {
    agent "claude"
    model "opus"
    prompt "You're a QA engineer. Write tests for the changed code."
}
```

Usage: `actor new foo --role qa` applies the role's agent + config +
prompt. Explicit CLI flags (`--agent`, `--model`, `--config`, positional
prompt / stdin) override the role. `agent` and `prompt` are promoted to
top-level fields; every other child is stored as a config key (values
coerced to strings).

### Per-agent defaults

`defaults "claude" { … }` / `defaults "codex" { … }` blocks set
defaults that apply to every actor of that kind:

```kdl
defaults "claude" {
    use-subscription true
    permission-mode "auto"
    model "opus"
}

defaults "codex" {
    m "o3"
    sandbox "workspace-write"
}
```

All keys live in one flat namespace. Each key is routed at parse time
by checking the agent class's `ACTOR_DEFAULTS` whitelist:

- **Whitelisted keys** (e.g. `use-subscription`) are actor-sh
  interpreted — they're never forwarded to the agent binary. The
  whitelist per agent is `ACTOR_DEFAULTS` on the Agent subclass
  (currently just `use-subscription` for both agents).
- **Everything else** becomes a CLI flag on the agent binary. Claude
  uses semantic long flags: `permission-mode "auto"` →
  `--permission-mode auto`. Codex uses native flag names verbatim:
  1-character keys become short flags (`m "o3"` → `-m o3`, `a "never"`
  → `-a never`), longer keys become long flags (`sandbox
  "workspace-write"` → `--sandbox workspace-write`). Unknown agent-arg
  keys are forwarded as-is, the agent binary decides whether they're
  valid.

A `null` value cancels a lower-precedence default:

```kdl
defaults "claude" {
    permission-mode null   # drop the built-in "auto" default
}
```

Merge precedence at actor creation (`actor new`), lowest → highest:
class `AGENT_DEFAULTS` + `ACTOR_DEFAULTS` (hardcoded on the Agent
subclass) → user kdl `defaults` block → project kdl `defaults` block →
role config (`--role`) → CLI `--config key=value`. The
resolved merge is snapshotted into the DB at creation; later edits to
`settings.kdl` don't retroactively change existing actors — use `actor
config <name> key=value` to mutate an actor's stored config. At run
time (`actor run`), the stored config is the base and per-run
`--config` arguments layer on top for that run only. `null` at a higher
layer cancels lower defaults; the emitter drops keys whose final value
is `None`.

Built-in class defaults today:

- `ClaudeAgent.AGENT_DEFAULTS = {"permission-mode": "auto"}` and
  `ClaudeAgent.ACTOR_DEFAULTS = {"use-subscription": "true"}`.
- `CodexAgent.AGENT_DEFAULTS = {"sandbox": "danger-full-access", "a":
  "never"}` and `CodexAgent.ACTOR_DEFAULTS = {"use-subscription":
  "true"}`.

Unknown top-level nodes (e.g. `alias`) are silently ignored for
forward-compat with follow-up tickets. A `defaults { ... }` block
inside a role is rejected with a helpful error pointing users at the
per-agent `defaults "<name>" { ... }` shape. Two legacy shapes are
hard-rejected with migration errors: the old `agent "<name>" {
defaults { ... } }` block (now flat `defaults "<name>" { ... }`) and
the old `template "<name>" { ... }` node (now `role "<name>" {
... }`).

Load programmatically via `actor.config.load_config(cwd=..., home=...)` —
both args default to `Path.cwd()` / `$HOME` so tests can inject temp dirs.

### Lifecycle hooks

A top-level `hooks { }` block declares shell commands that fire around
actor lifecycle events. Each value runs via `/bin/sh -c`, inheriting the
caller's env plus `ACTOR_NAME`, `ACTOR_DIR`, `ACTOR_AGENT`, and (when
set) `ACTOR_SESSION_ID`. Cwd is the actor's worktree.

```kdl
hooks {
    on-start   "kubectl config use-context dev"
    before-run "git fetch --quiet"
    after-run  "./scripts/notify.sh"
    on-discard "git diff --quiet && git diff --quiet --staged"
}
```

- `on-start` — fires once during `actor new`, after the actor row is
  recorded, before returning. Non-zero rolls back the row + worktree.
- `before-run` — fires before every `actor run` (incl. interactive),
  before the Run row is inserted. Non-zero aborts the run.
- `after-run` — fires after the run completes and the DB row has been
  updated with final status. Receives three extra env vars:
  `ACTOR_RUN_ID`, `ACTOR_EXIT_CODE`, `ACTOR_DURATION_MS`. Non-zero
  logs a warning but does NOT fail the completed run — there's nothing
  to roll back.
- `on-discard` — fires during `actor discard`, after cleanup (running
  agents stopped), before the DB row is deleted. Non-zero aborts
  discard unless `actor discard --force` / `-f` is passed. If the
  worktree is gone, the hook runs from `$HOME` instead and `ACTOR_DIR`
  still reports the missing path so the script can detect it.

Project hooks override user hooks per event (same merge rule as
roles).

## Watch theme

The `actor watch` TUI ships with `claude-dark` / `claude-light`. If it
detects omarchy running locally (presence of
`~/.config/omarchy/current/theme/colors.toml`), it **flavors** the
active claude-dark/light theme with omarchy's palette so the TUI reads
as part of the user's desktop:

- `foreground` ← `colors.toml`'s `foreground`
- `background` ← `colors.toml`'s `background`
- `surface`, `panel` ← `background` lifted ~8% toward `foreground` so
  panels stay subtly distinct from the desktop bg (still palette-derived)
- `secondary` ← `colors.toml`'s `accent` (brand/logo slot)
- `primary`, `accent` ← `hyprland.conf`'s `$activeBorderColor` (focus
  rings match active-window border)

Semantic slots (`warning` / `error` / `success`) stay as the base
theme had them — `colors.toml` doesn't carry semantic meaning.

**Live reload:** every 3 seconds we re-stat the resolved-target mtime of
`colors.toml`. If it changed (e.g. the user ran `omarchy theme set
<name>`), we rebuild the flavor and re-register under the same theme
name. For instant updates, `actor setup --for omarchy` installs a
one-line fragment into `~/.config/omarchy/hooks/theme-set` that sends
`SIGUSR2` to any running `actor watch`, triggering an immediate reload.
The SIGUSR2 handler is wired unconditionally — the setup command only
adds the hook that sends the signal.

See `src/actor/watch/omarchy_theme.py` for the flavor logic. Malformed
TOML logs a warning and keeps whatever theme is active rather than
crashing the TUI.

## Interactive mode

Both the CLI and the watch TUI can open a live Claude/Codex session for
an existing actor.

- CLI: `actor run <name> -i` inherits the caller's TTY via subprocess.Popen
  (stdin/stdout/stderr passthrough). Tracked as a Run with prompt
  `*interactive*` so it shows up in `actor show`.
- Watch: select an actor in the tree and press Enter. The detail pane
  swaps to a TerminalWidget backed by a forked PTY (see
  `src/actor/watch/interactive/`). Ctrl+Z leaves the widget but keeps
  the subprocess alive; quitting watch SIGTERMs everything.

The watch integration is structured so the pure parts (screen, input,
batcher, diagnostics) are unit-testable with synthetic inputs, and the
impure parts (PtySession, widget) are integration-tested with real
`/bin/cat` and `/bin/sh` subprocesses. Ctrl+Shift+D inside `actor watch`
dumps the DiagnosticRecorder ring buffer to stderr for post-mortems.

## Architecture notes

- Commands are pure functions that take a `Database` + interfaces and return strings. Side effects go through the `Agent`, `GitOps`, and `ProcessManager` ABCs — this is what makes everything testable with fakes.
- Requires Python 3.10+. Runtime deps: `kdl-py` (config parser), `mcp` (MCP server), `pyte` / `textual` / `textual-serve` (watch dashboard + embedded TTYs).
- Actors spawned by other actors are tracked via the `parent` column. The `ACTOR_NAME` env var is set before launching an agent, so child actors automatically record their parent. `discard` cascades recursively — stops running children, then deletes.
- DB migrations run on open (see `db.py` after schema creation). New columns are added via `ALTER TABLE` if missing.
