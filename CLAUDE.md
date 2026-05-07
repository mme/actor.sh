# actor.sh

Manages multiple Claude/Codex agents running in isolated git worktrees.

## Status: pre-release

actor.sh has not had a public release yet. There are no users to keep
on an old shape. **Do not add backwards-compatibility mechanisms** —
no soft aliases, no deprecation warnings, no migration errors, no
shims for renamed flags or moved config nodes. Rename, restructure,
delete; update tests and docs to match. The codebase should always
read as if the current shape is the only one that ever existed.

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

Launch the orchestrator session (claude with the `main` role applied
and the actor channel enabled):

```bash
actor main                                        # appends main role's prompt + adds channel flag
actor main "kick off the refactor"                # one-shot
actor main --model opus                           # forwards trailing args to claude
```

`actor main` resolves the `main` role from settings.kdl (built-in if
not overridden), takes its `prompt` as `--append-system-prompt`, and
execs `claude --dangerously-load-development-channels server:actor
[args...]`. Override the built-in by adding `role "main" { ... }` to
`~/.actor/settings.kdl` (user) or `<repo>/.actor/settings.kdl`
(project). Today only `agent "claude"` is supported on this command;
overriding the role to use codex fails with a clear error.

Sub-claudes spawned by actors inherit the channel flag automatically
(see `ClaudeAgent._CHANNEL_ARGS`), so nested actors can receive
completion notifications too.

### `/actor` slash command

The deployed skill (`src/actor/_skill/SKILL.md`, frontmatter
`name: actor`) is also a user-typable slash command. The user can
type `/actor` for a quick status, `/actor stop fix-nav` for a direct
lifecycle op, or `/actor spin up a reviewer to look at the auth
module` for a natural-language task. The skill's "Slash invocation"
section interprets `$ARGUMENTS` and routes to the right MCP tool.

Both invocation paths (model-driven discovery and user-typed slash)
work — `disable-model-invocation` is intentionally NOT set in the
frontmatter.

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

### Soak test (pre-release / pre-merge confidence check)

`tests/test_soak.py` exercises the daemon under sustained simulated load
and asserts memory / FD / log-rotation behaviour at the end. It does NOT
run as part of the default suite — `unittest discover` skips it.

```bash
ACTOR_RUN_SOAK=1 uv run python -m unittest tests.test_soak  # ~30 min
SOAK_DURATION=86400 ACTOR_RUN_SOAK=1 \
  uv run python -m unittest tests.test_soak                  # 24h
```

Knobs (env vars, all optional):
- `SOAK_DURATION` — seconds (default 1800 = 30 min).
- `SOAK_ACTOR_COUNT` — concurrent actors per cycle (default 5).
- `SOAK_METRICS_PATH` — CSV destination (default `/tmp/actord-soak-metrics.csv`).

Each cycle creates `SOAK_ACTOR_COUNT` actors with a fake-claude run, lists,
shows, and discards them; a separate subscriber receives notifications and
periodically simulates a network blip to exercise auto-reconnect. Metrics
land in the CSV every minute (RSS / FD count / connection count / log size /
db size). Final assertions: daemon alive, RSS within 50MB of warmup,
FD count within ±10% of post-warmup, every notification delivered, log
rotated correctly when it crosses 10MB.

### End-to-end TUI tests with Textual's Pilot

**Default to writing real e2e tests for any user-visible behavior.** A
unit test that asserts on internal state can pass while the actual UX is
broken — too many bugs hide between "the function returned the right
value" and "the user pressed a key and the right thing happened."

Textual ships a headless harness (`App.run_test()`) that drives our app
exactly like a user. There's no real terminal — Textual swaps in a
`HeadlessDriver` that captures rendered frames into memory. The returned
`Pilot` object exposes `press(...)`, `click(...)`, `pause(...)` etc.
Outside pilot calls, `app.focused`, `app.query_one(...)`, widget
properties — all live and assertable.

**Canonical example:** `tests/test_watch_navigation.py` covers the
`actor watch` arrow-key navigation. `tests/test_interactive_widget.py`
covers the embedded TerminalWidget against a real `/bin/cat` PTY.

**The pattern:**

```python
class WatchSomething(unittest.IsolatedAsyncioTestCase):
    async def test_something(self):
        app = ActorWatchApp(animate=False)  # animate=False skips splash delay
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("a")              # focus actor tree
            await pilot.pause(0.05)             # let the app process
            self.assertIs(app.focused, app.query_one(ActorTree))
```

**Things I learned the hard way:**

- **Async glue is just `IsolatedAsyncioTestCase`** — each test method is
  `async def` and `await`s the pilot. No event-loop boilerplate.
- **Don't sleep on a fixed timer — poll a condition.** If the app does
  work in `on_ready` (DB fetch, layout settle), wait by re-checking
  state after a small `pilot.pause(0.05)` instead of a hopeful
  `pilot.pause(2)`. Same pattern as the existing `interactive_widget`
  tests.
- **Splash screens block input.** Boot with `animate=False` (we wired
  this up specifically for tests) or wait for `app._splash_active` to
  flip before pressing anything.
- **State isolation is on you.** `App.run_test` doesn't mock the
  filesystem or env. For DB-backed tests, patch `HOME` to a tempdir +
  insert fixture rows so each test reads from a fresh state. See
  `_setup_home()` in `tests/test_watch_navigation.py`.
- **Mutation-test your e2e tests.** A passing test isn't proof — flip
  the production code temporarily and re-run; if the test still passes,
  it's theatre and you need a stronger assertion. Found this out the
  hard way: my arrow-nav runtime tests passed even when I removed
  `priority=True` from the binding (Textual's RichLog `wrap=True`
  behaves more leniently than I assumed). The compile-time guard
  (asserting on `BINDINGS`) caught the regression where the runtime
  tests didn't. Both kinds have their place.

Reference: [Textual docs — Testing](https://textual.textualize.io/guide/testing/) covers
`Pilot`, the headless driver, and the snapshot-test pattern.

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
role "reviewer" {
    description "Concise code review; flag bugs and style issues."
    agent "claude"
    model "opus"
    prompt "You are a senior code reviewer. Be concise; flag bugs, security issues, and style violations. Don't fix anything — report findings only."
}
```

Usage: `actor new auth-review --role reviewer "Review src/auth/*.py for security issues"`.

The role's `prompt` is the actor's **system prompt** (the role's
identity / behavioral guidance), injected as `--append-system-prompt`
for claude actors. The CLI's positional prompt is the **task**. They
coexist — `--append-system-prompt` layers the role's identity on top
of Claude Code's defaults; the task tells the actor what to do.

`agent`, `prompt`, and `description` are promoted to top-level fields;
every other child is stored as a config key (values coerced to
strings). Explicit CLI flags (`--agent`, `--model`, `--config`)
override the role's values for those slots; the per-call prompt is the
task (it doesn't compete with the role's system prompt).

Codex doesn't yet support role-level system prompts. A role with `prompt`
and `agent "codex"` is rejected at `actor new` time with a clear error.

To see what's defined, run `actor roles`. If a role-name typo lands in
`actor new --role <bad>`, the error lists the available names.

A built-in `main` role exists by default — the main actor
preset used by `actor main`. Override it with a `role "main" { ... }`
block in settings.kdl to swap in your own main actor system prompt
(whole-role replacement; there is no per-field merge).

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
per-agent `defaults "<name>" { ... }` shape.

Load programmatically via `actor.config.load_config(cwd=..., home=...)` —
both args default to `Path.cwd()` / `$HOME` so tests can inject temp dirs.

### Ask block

A top-level `ask { }` block holds free-form natural-language guidance
that gets appended to the corresponding MCP tool descriptions at server
startup. The orchestrator reads them as part of its tool catalog and
decides when to use `AskUserQuestion` before acting.

```kdl
ask {
    on-start   "Always confirm the agent kind for risky tasks."
    before-run "Skip questions; assume per-run config never changes."
    on-discard null   # silence the default — discard never asks
}
```

Valid keys: `on-start` (appended to `new_actor`), `before-run`
(appended to `run_actor`), `on-discard` (appended to `discard_actor`).
No `after-run` — there's nothing to ask after a run completes.

Per-key resolution:

- key absent → fall through to the hardcoded default (orchestrator's
  baseline guidance for that tool)
- value is a string → append that string verbatim
- value is `null` or `""` → user opt-out; append nothing

Tool descriptions are static per MCP-server lifetime. Edits to
`settings.kdl` apply on the next `actor main` (re-exec to pick them
up). Project-level `ask` blocks override user-level per key, same merge
rule as templates and hooks.

The hardcoded defaults live in `ASK_DEFAULTS` in `src/actor/config.py`.

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

### What discard does NOT clean up

`actor discard` removes the worktree and the DB row, but
**intentionally leaves the underlying git branch in place**. The
default `on-discard` hook (`git diff --quiet`) only catches unstaged
modifications — committed work would be silently destroyed if the
branch were force-deleted on discard. The trade-off is that
`actor new <same-name>` after discard fails with "branch already
exists"; the user recovers with `git branch -D <name>` in the source
repo (after confirming the branch's commits are merged or
unwanted), or by picking a different name for the new actor.

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
- Requires Python 3.10+. Runtime deps: `kdl-py` (config parser), `mcp` (MCP server), `pyte` / `textual` (watch dashboard + embedded TTYs). Port 2204 is reserved for the upcoming actord daemon (issue #35) and is intentionally not bound by anything in-tree today.
- Actors spawned by other actors are tracked via the `parent` column. The `ACTOR_NAME` env var is set before launching an agent, so child actors automatically record their parent. `discard` cascades recursively — stops running children, then deletes.
- DB migrations run on open (see `db.py` after schema creation). New columns are added via `ALTER TABLE` if missing.
