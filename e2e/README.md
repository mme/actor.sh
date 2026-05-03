# actor.sh end-to-end test suite — plan

## Goal

A test suite that exercises actor.sh **the way a user does**: through
the CLI, the MCP wire, and the `actor watch` TUI — against fake
`claude` and `codex` binaries that behave realistically (write the
right log files, accept the same flags, sleep / fail / answer on
demand). Tests pass means "the user-visible product works"; tests
fail means "the user would notice."

Concretely, this suite catches things the unit suite can't:

- A command works in isolation but the CLI plumbing drops a flag.
- The MCP tool returns the right string but the channel notification
  never arrives.
- Two actors complete simultaneously and one notification gets
  mis-routed.
- A discard cascade leaves orphan worktrees.
- A new role.prompt change breaks how Claude is launched.
- A TUI re-render after a poll changes which widget has focus and
  steals input from the user.

## Non-goals

- **Not** a stand-in for unit tests. Internal-state assertions stay
  in `tests/`. e2e/ tests the user-visible boundary.
- **Not** a hermetic test of real Anthropic / OpenAI behavior. The
  fakes are stand-ins for the agent CLIs, not the agents themselves.
- **Not** GUI snapshot testing. We assert on widget state via Pilot,
  not pixel diffs.
- **Not** running the real `actor setup --for claude-code` against
  the user's actual `~/.claude/`. Each test isolates HOME.

## Architecture overview

```
e2e/
  README.md                 ← this file
  conftest.py               ← shared fixtures (HOME isolation, fake-bin path, factory)
  fakes/
    fake_claude.py          ← drop-in replacement for `claude` (Python script)
    fake_codex.py           ← drop-in replacement for `codex`
    bin/
      claude → ../fake_claude.py    (symlink with shebang dispatch)
      codex  → ../fake_codex.py
    fixtures/
      response_*.jsonl      ← canned response payloads keyed by name
      log_*.jsonl           ← canned session-log frames
  harness/
    isolated_home.py        ← tempdir HOME with .actor/ + ~/.claude/ scaffolding
    cli.py                  ← `run_actor_cli([args...])` helper, captures stdout/stderr/exit
    mcp_client.py           ← stdio MCP client that initializes + lists tools + calls them
    pilot.py                ← bootstrap helpers for ActorWatchApp under Pilot
    fakes_control.py        ← env-var setters: sleep, exit code, response, raise
  tests/
    cli/
      test_new.py
      test_run.py
      test_list.py
      test_show.py
      test_logs.py
      test_stop.py
      test_discard.py
      test_config.py
      test_roles.py
      test_main.py            ← exec'd actor main → smoke check that claude is launched
      test_setup.py           ← actor setup / actor update against an isolated HOME
    mcp/
      test_handshake.py
      test_list_actors.py
      test_show_actor.py
      test_logs_actor.py
      test_new_actor.py
      test_run_actor.py
      test_stop_actor.py
      test_discard_actor.py
      test_config_actor.py
      test_list_roles.py
      test_channel_notifications.py
      test_ask_block.py       ← verify ask appendix on the wire
    tui/
      test_splash.py
      test_tree_navigation.py
      test_tab_cycling.py
      test_overview_pane.py
      test_diff_pane.py
      test_logs_streaming.py
      test_command_palette.py
      test_help_overlay.py
      test_confirm_dialog.py
      test_interactive_mode.py
      test_status_polling.py
      test_theming.py
      test_multi_actor_state.py
    workflows/
      test_orchestrator_session.py    ← actor main → spawn sub-actor via MCP → channel
      test_parallel_actors.py
      test_parent_child_cascade.py
      test_role_application.py
      test_hook_lifecycle.py
      test_failure_recovery.py
```

The `e2e/` directory is its own unittest tree — `python -m unittest
discover e2e` runs the whole thing. Reuses `unittest.TestCase` and
`unittest.IsolatedAsyncioTestCase` (matches the existing `tests/`
style; no new framework).

### How fakes get injected

Two layers:

1. **`PATH` prepend** — `e2e/fakes/bin/` is prepended to `PATH` in the
   test's environment. Anything that does `subprocess.Popen(["claude",
   ...])` finds the fake first. Covers everything in
   `src/actor/agents/claude.py` and `codex.py` plus the
   `actor main` exec path.
2. **Optional override** — `actor.interfaces.binary_exists` is the
   choke point used to detect a missing CLI; tests can also patch it
   directly when they need to assert the "missing binary" path
   without removing the fake from `PATH`.

The fakes are real Python scripts with `#!/usr/bin/env python3`
shebangs (or installed via `uv` if the harness needs deps). They:

- Accept the same flags the real binaries do (well-known subset).
- Read env vars to control behavior (sleep, exit code, output).
- Write JSONL session logs to the same path real claude/codex would.
- Print the same stdout shape (e.g. final assistant text).

Test code never inspects the fake script's source — it controls
behavior via env vars set in the per-test fixture.

## Fake claude — shape & contract

### Flags accepted (no-op or behaviorally significant)

| Flag | Behavior |
| --- | --- |
| `--dangerously-load-development-channels server:actor` | no-op (accept silently) |
| `-p` (print mode) | switches to non-interactive mode (default for actor.sh sub-actors) |
| `--session-id <id>` | writes log to `~/.claude/projects/<encoded-cwd>/<id>.jsonl` |
| `--resume <id>` | appends to existing session-id log |
| `-c`, `--continue` | resume the most recent session in the cwd |
| `--system-prompt <s>` | record in log frame; treat as identity |
| `--append-system-prompt <s>` | record in log frame; affects fake's "personality" branching |
| `--model <m>` | record in log frame; available for assertions |
| `--permission-mode <m>` | record in log frame |
| `--config <kv>` | record |
| `--` | everything after is the prompt |
| positional `<prompt>` | the prompt |

### Behavior knobs (env vars set per-test)

| Env var | Effect |
| --- | --- |
| `FAKE_CLAUDE_EXIT=N` | Exit with code N after writing logs. Default 0. |
| `FAKE_CLAUDE_SLEEP=SECS` | Sleep before exit (simulates a long run). Default 0. |
| `FAKE_CLAUDE_RESPONSE=<text>` | Final assistant text. Default: echo of the prompt. |
| `FAKE_CLAUDE_RESPONSE_FILE=<path>` | Read response from a file (multi-line, with tool-use frames if present). |
| `FAKE_CLAUDE_CRASH=signal` | Raise the named signal before completion (test stale-pid handling). |
| `FAKE_CLAUDE_TOOLS=<json-list>` | Inject synthetic tool_use frames into the log. |
| `FAKE_CLAUDE_THINKING=<text>` | Inject a thinking frame. |
| `FAKE_CLAUDE_SPAWN_CHILD=<cmd>` | After completion, spawn a child process. Used to simulate nested actors. |
| `FAKE_CLAUDE_LOG_DIR=<path>` | Override default `~/.claude/projects/...` location. Default uses `$HOME/.claude/...`. |

### JSONL log format

Matches what `src/actor/agents/claude.py:_parse_log_dict` expects
today. One record per line. Minimum frame set:

```jsonl
{"type":"system","message":{"text":"Session started"},"timestamp":"2026-05-03T10:00:00Z"}
{"type":"user","message":{"content":"<the prompt>"},"timestamp":"..."}
{"type":"assistant","message":{"content":[{"type":"text","text":"<the response>"}]},"timestamp":"..."}
```

Optional frames (configurable via env):
- `tool_use` content blocks with name/input
- `tool_result` blocks
- `thinking` blocks
- `usage` blocks (token counts)

Path is `$HOME/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
where `encoded-cwd` matches `ClaudeAgent._encode_dir`.

## Fake codex — shape & contract

Same idea as fake claude, with codex's flag surface and rollout
format. Specifically:

### Flags accepted

| Flag | Behavior |
| --- | --- |
| `-c <kv>` | record (configuration override) |
| `-m <model>` | record |
| `-a <approval>` | record (approval policy) |
| `-s <sandbox>` / `--sandbox <mode>` | record |
| `-i <file>` | image attachment (no-op record) |
| `-C <dir>` / `--cd <dir>` | cwd override |
| `--add-dir <dir>` | record |
| `exec` subcommand | non-interactive mode; reads prompt from arg or stdin |
| `resume` subcommand | resume session from rollout |
| `--last` (with resume) | continue most recent |

### Behavior knobs

`FAKE_CODEX_EXIT`, `FAKE_CODEX_SLEEP`, `FAKE_CODEX_RESPONSE` etc. —
mirror the claude set. Plus:

- `FAKE_CODEX_ROLLOUT_DIR` — where to write rollout JSONL. Default
  matches codex's real path.

### Rollout format

Matches what `src/actor/agents/codex.py` (the parser added in
`bad3193`) expects. Modern rollout shape with `tool_call`,
`tool_call_output`, `agent_message`, `reasoning`, `event_msg`,
`token_count` records.

## Test harness

### Per-test isolation (`harness/isolated_home.py`)

Every e2e test starts in a clean tempdir:

```
$TMPDIR/<test-id>/
  home/                    ← $HOME, prepended to PATH gets fakes
    .actor/
      actor.db             ← created on first DB open
      worktrees/           ← created by GitOps on first new_actor
      settings.kdl         ← optional, written per-test
    .claude/
      projects/            ← fake claude writes session logs here
  cwd/                     ← test's working directory; usually a git repo
    .git/                  ← initialized by harness (real git, not faked)
```

A context manager handles setup + teardown:

```python
with isolated_home() as env:
    env.write_settings_kdl("role \"qa\" { ... }")
    env.run_cli(["actor", "new", "alice", "do x"])
    actor = env.show_actor("alice")
    assert actor.status == "done"
```

`env.run_cli([...])` sets `HOME`, `PATH` (with fakes prepended), and
runs the CLI subprocess; captures stdout/stderr/exit. `env.show_actor`
opens the test's DB read-only and returns a parsed Actor.

### MCP client (`harness/mcp_client.py`)

A minimal stdio MCP client: spawns `actor mcp` as a subprocess in the
isolated env, does the JSON-RPC handshake, exposes `list_tools()`,
`call_tool(name, args)`, and `recv_notification(timeout)` for channel
events. Used by every `e2e/tests/mcp/` test.

### Pilot bootstrap (`harness/pilot.py`)

Wraps `ActorWatchApp.run_test()` with the isolated HOME / fake-bin
setup pre-applied. Provides:

- `boot_watch(env, *, pre_actors=[...]) -> AsyncContextManager[(app, pilot)]`
- `select_actor(pilot, app, name)` — moves cursor + posts NodeSelected
- `wait_for_status(pilot, app, name, status)` — polls until the tree
  shows the actor in the given status
- `assert_focused(app, widget_or_id)` — pretty assertion error

### Fakes control (`harness/fakes_control.py`)

Helpers for setting per-call behavior of the fakes:

```python
with claude_responds("Done in 2s", sleep=2.0, exit=0):
    env.run_cli(["actor", "new", "alice", "do thing"])
```

Sets the env vars before subprocess spawn, restores after.

## Coverage matrix — what to test

Each entry below maps to one test file (or a class within one). The
goal is for the explicit list to be the spec — when a feature is
added, the e2e plan extends with new entries.

### CLI surface (`e2e/tests/cli/`)

- **`actor new`**
  - `[done] new <name>` (idle actor created, no run started)
  - `[done] new <name> "<prompt>"` (creates + runs in foreground; reports completion)
  - `new <name>` reading prompt from piped stdin
  - `new <name> --agent codex "<prompt>"` (wires the fake codex)
  - `new <name> --no-worktree` (runs in cwd, no branch creation)
  - `new <name> --base <branch>` (worktree forks from named branch)
  - `new <name> --dir <path>` (creates worktree in another repo)
  - `new <name> --config model=opus` (saved as actor default)
  - `new <name> --use-subscription / --no-use-subscription`
  - `new <name> --role <role>` (role applied; system prompt forwarded; no auto-run without prompt)
  - `new <name> --role <role> "<task>"` (role + task; both passed to claude)
  - `new <name> --role <unknown>` → error lists available
  - `new <name> --role <codex-role-with-prompt>` → error message about codex
  - `new <duplicate-name>` → already-exists error
  - `new <invalid/name>` → invalid-name error
  - First `new` runs `on-start` hook (if defined); failure rolls back actor + worktree
  - Hook env vars (ACTOR_NAME, ACTOR_DIR, ACTOR_AGENT) reach the hook process

- **`actor run`**
  - `run <name> "<prompt>"` (reuses existing session, fakes resume properly)
  - `run <name>` reads stdin
  - `run <name> -i` enters interactive mode (PTY-based; verify caller's tty is forwarded)
  - `run <name> --config model=haiku "<x>"` (per-run override; not persisted)
  - `run <name>` on a running actor → `IsRunning` error
  - `run <name>` triggers `before-run` hook; failure aborts run with no Run row
  - `after-run` hook fires with ACTOR_RUN_ID, ACTOR_EXIT_CODE, ACTOR_DURATION_MS
  - `after-run` hook failure logs warning but doesn't fail the completed run

- **`actor list`**
  - empty DB → header only
  - mixed statuses (running, done, error, idle, stopped) display correctly
  - `--status running` filter
  - stale-pid actor reclassified as ERROR

- **`actor show`**
  - existing actor: name, dir, agent, config, recent runs
  - `--runs N`: N recent runs shown
  - `--runs 0`: details only
  - missing actor → not-found error

- **`actor logs`**
  - latest run's output
  - `--verbose`: tool_use + thinking + timestamps included
  - `--watch`: streams new frames as fake claude appends (sleep + write more)
  - actor with no runs → friendly "no runs yet"

- **`actor stop`**
  - running actor → SIGTERM sent, status flips to STOPPED
  - idle actor → no-op or "not running"

- **`actor discard`**
  - clean tree (no diff): proceeds, deletes actor + worktree
  - dirty tree: default `git diff --quiet` hook fails, discard aborts
  - dirty tree + `--force`: discard proceeds anyway
  - parent with children: leaves discarded first, then parent
  - on-discard hook failure on parent stops cascade
  - missing worktree: hook runs from $HOME

- **`actor config`**
  - view (no pairs)
  - update (single pair)
  - update (multiple pairs)
  - update with `key=` (clear value)
  - on RUNNING actor: change applies on next run, not in-flight

- **`actor roles`**
  - empty (only built-in main): table includes main row
  - with user roles: sorted, name + agent + description columns
  - with description silenced (no description set): empty cell

- **`actor main`**
  - launches fake claude with `--dangerously-load-development-channels server:actor`
  - `--append-system-prompt` is the resolved main role's prompt
  - trailing args forwarded to fake claude
  - if main role overridden in settings.kdl, custom prompt used
  - main role with `agent codex` → clear error

- **`actor setup`** / **`actor update`**
  - fresh setup --for claude-code: registers MCP, copies skill files to ~/.claude/skills/
  - skill files match the bundled `actor._skill` contents (no drift)
  - `actor update` refreshes versioned auto-block in deployed SKILL.md
  - `--scope project` uses .claude/skills/ in cwd instead of $HOME

### MCP wire (`e2e/tests/mcp/`)

Each test spawns `actor mcp` as a subprocess and drives JSON-RPC.

- **Handshake**
  - initialize → serverInfo, capabilities
  - initialized notification accepted
  - tools/list returns the expected names
  - tool descriptions include the ask-block appendix (default + customized)

- **list_actors**
  - empty DB
  - with several actors of varied status
  - status filter
  - returns same text as the CLI's output

- **show_actor**, **logs_actor**, **config_actor** — same shape as CLI tests

- **new_actor**
  - simple create returns "created" message immediately
  - create + prompt fires background run; channel notification arrives on completion
  - `role="..."` applies role; system prompt is in fake claude's invocation
  - `dir="<absolute>"` puts worktree in the right place
  - missing actor name → actor error returned in result, not raised

- **run_actor**
  - fires background run; channel notification arrives
  - per-call config overrides flow into the fake claude's invocation
  - returns immediately (response time bounded)

- **stop_actor** / **discard_actor**
  - stop: subsequent list_actors shows STOPPED
  - discard: actor disappears from list_actors; worktree gone from disk
  - discard a running actor: stops first, then deletes
  - discard parent: cascade observable in list_actors snapshot

- **list_roles**
  - returns table including built-in main
  - reflects user-defined roles in settings.kdl

- **Channel notifications**
  - completion notification: actor name, status, output present
  - status field flips correctly on stop / discard / error
  - parallel completions: each notification has the right actor name (no mis-routing)
  - notification fires within N seconds of run completion

- **Ask block on the wire**
  - default appendix appears in new_actor / run_actor / discard_actor descriptions
  - user-provided override replaces default
  - `null` / `""` silences default
  - per-key project-overrides-user merge

### TUI (`e2e/tests/tui/`)

Pilot-driven. Boot `ActorWatchApp` against the isolated env.

- **Splash → main view**
  - splash visible on mount; transitions to main after `animate=False` boot
  - Footer shows expected bindings post-splash
  - Tree gets initial focus

- **Tree navigation**
  - up / down moves cursor
  - left / right (and Ctrl+B / Ctrl+F) cycle focus correctly per #24
  - Enter on actor with idle session → enters interactive mode
  - Enter on actor with no session → no-op or hint
  - `a` key returns focus to tree from anywhere

- **Tab cycling**
  - `o` selects OVERVIEW
  - `d` selects DIFF
  - left/right cycles through tabs when detail pane focused
  - tab-bar `Tabs` widget gets focus, can be navigated

- **Overview pane**
  - selecting an actor populates header (name, agent, status icon)
  - "running for Xs" updates each second when actor is RUNNING
  - non-default config keys appear; defaults are hidden
  - logs RichLog scrolls; PageUp/Down work

- **Diff pane**
  - shows nothing for actors without a worktree
  - shows summary for clean worktree
  - shows per-file diff for dirty worktree
  - badge label updates as actor writes files

- **Logs streaming**
  - new lines appended to the JSONL appear in OVERVIEW within poll interval
  - cache invalidates when actor's session_id changes (per #63)

- **Command palette**
  - `p` opens palette
  - "Stop alice" command stops the selected actor (after confirm)
  - "Discard alice" command opens confirm dialog; Yes deletes, No cancels
  - Keys overlay (`?`) shows the curated keymap

- **Confirm dialog**
  - opens on discard from palette
  - escape / No closes without action
  - Yes triggers the action
  - focus is on the confirm button (not no-op)

- **Interactive mode**
  - `i` from a non-running actor enters interactive
  - PTY runs fake claude in `--continue` mode
  - keystrokes in the widget reach the fake (echoed back through pyte)
  - Ctrl+Z exits widget but session lives
  - quitting watch SIGTERMs interactive sessions

- **Status polling**
  - actor flips DONE → tree icon updates within poll interval
  - actor crashes → tree icon flips to ERROR within poll interval
  - new actor created externally (via CLI in another process) appears in tree

- **Theming**
  - claude-dark applied by default
  - claude-light selectable
  - omarchy theme detected and flavored (when omarchy fixture present)

- **Multi-actor state**
  - 3 actors in different statuses render correctly
  - selecting each shows the right detail pane
  - stop on the focused actor doesn't break tree focus / cursor
  - discarded actor disappears from tree, cursor lands on a sibling

### Workflows (`e2e/tests/workflows/`)

End-to-end flows that span multiple subsystems.

- **Orchestrator session**
  - launch `actor main` (subprocess) → MCP server starts → list_roles works
  - orchestrator calls `new_actor(role="reviewer", prompt="...")` → sub-actor spawned
  - completion notification reaches the orchestrator's claude session
  - sub-actor's worktree created from orchestrator's cwd

- **Parallel actors**
  - spawn 5 actors in quick succession; each completes; 5 notifications received
  - notifications carry correct names (no mis-routing under load)
  - tree updates show all 5 statuses correctly during overlapping execution

- **Parent-child cascade**
  - actor A spawns actor B (via fake-claude that calls actor CLI)
  - parent column on B references A
  - discard A → B is discarded first (leaves-first ordering)
  - discard with running B → B is stopped before discard

- **Role application**
  - define a `reviewer` role; create actor with it
  - fake claude receives `--append-system-prompt <role.prompt>`
  - per-call prompt is the task, not the role's prompt
  - explicit `--config model=haiku` beats role's `model "opus"`

- **Hook lifecycle**
  - on-start fires once at create
  - before-run fires before each run
  - after-run fires with run metadata
  - on-discard fires before delete
  - hook failure semantics per spec
  - project hooks override user hooks per event

- **Failure recovery**
  - actor's claude exits non-zero → status ERROR, run row has exit_code
  - actor's claude killed by SIGKILL → stale-pid detection flips to ERROR on next list
  - mid-run database corruption → graceful error, no half-state
  - worktree creation fails (target exists, no permission) → no actor row created
  - on-start hook fails → actor row + worktree rolled back atomically

## Failure modes / edge cases — explicit list

To make sure these aren't forgotten:

- Unicode in actor name (rejected at validation, clear message)
- Unicode in prompt (passes through fakes intact)
- Actor name with valid hyphens vs. invalid characters
- Prompt with shell metacharacters (`$VAR`, backticks, semicolons) — passed via execvp, no shell, byte-exact
- Long prompt (~10 KB, ~100 KB) — fits in argv
- Concurrent CLI calls from two shells (DB lock semantics)
- Zero-byte stdin → friendly "expected a prompt" error
- Settings.kdl malformed → clear error pointing at file + line
- Settings.kdl with unknown keys silently ignored (forward-compat)
- Settings.kdl deleted between create and run → run uses snapshotted config
- Settings.kdl edited mid-session → MCP server keeps loaded copy until restart (documented)
- HOME env unset → settings.kdl path resolution skips user layer
- Two actors with same source repo → branches don't collide
- Worktree path collides with existing dir → clean error
- Discard with worktree manually deleted → harmless, hook runs from $HOME
- Channel notification fails to send → logged, but actor row still updated correctly
- Fake claude binary missing → clear "claude not on PATH" error
- Fake claude SIGSEGVs mid-run → stale-pid handling, ERROR status
- Database file locked → wait + retry, eventually surface error if persists
- TUI: terminal resize during render (Pilot can simulate via app.refresh)
- TUI: focus moves while a poll is in flight (no exception)

## Running

```bash
# Whole e2e suite
uv run python -m unittest discover e2e

# One area
uv run python -m unittest discover e2e/tests/tui

# One file
uv run python -m unittest e2e.tests.cli.test_new

# With verbose
uv run python -m unittest discover e2e -v
```

The unit suite (`uv run python -m unittest discover tests`) stays
disjoint and fast (currently ~25s, ~655 tests). e2e/ is expected to
be slower — fakes spawn real subprocesses, fixtures init real git
repos. Aim for under ~60s for the full e2e run; if it grows beyond
that, parallelize with `pytest-xdist` (added if/when needed).

## CI integration

GitHub Actions matrix:

```yaml
- run: uv run python -m unittest discover tests
- run: uv run python -m unittest discover e2e
```

Both must pass before merge. e2e/ runs on the same Linux runner the
unit suite already uses; fakes are pure-Python so no extra system
deps. macOS/Windows runners deferred — current CI is Linux-only and
the unit suite already covers cross-platform-sensitive code.

## Phasing — milestones

A suite this big won't land in one PR. Suggested order (each is a
separate PR, each lands incrementally green):

1. **M1 — fakes + harness skeleton.** `e2e/fakes/fake_claude.py`,
   `fake_codex.py`, `harness/isolated_home.py`,
   `harness/cli.py`, plus 3 smoke tests proving the fakes are
   wired correctly. ~300 LOC.
2. **M2 — CLI coverage.** `e2e/tests/cli/test_new.py`,
   `test_run.py`, `test_list.py`, `test_show.py`. ~400 LOC.
3. **M3 — CLI lifecycle ops.** `test_stop.py`, `test_discard.py`,
   `test_config.py`, `test_roles.py`, `test_main.py`. ~400 LOC.
4. **M4 — MCP wire.** `harness/mcp_client.py` + all
   `e2e/tests/mcp/`. ~500 LOC.
5. **M5 — TUI smoke + navigation.** `harness/pilot.py` + tree /
   tab / overview / diff tests. ~500 LOC.
6. **M6 — TUI polish (palette, dialogs, interactive).** ~400 LOC.
7. **M7 — Workflows.** Orchestrator session, parallel actors,
   parent-child cascade, role application, hook lifecycle, failure
   recovery. ~500 LOC.
8. **M8 — Edge cases sweep.** Pull from the explicit list above;
   each is small but the long tail matters.

Total estimated scope: ~3,000-3,500 LOC across ~70-80 test files.
Roughly the size of the existing unit suite, but covering the
user-visible surface end to end.

## Open design questions

- **Pytest vs. unittest:** existing `tests/` uses unittest. Sticking
  with unittest keeps the toolchain unchanged. `pytest-xdist` for
  parallelization can be added without converting tests if e2e/ stays
  pytest-discoverable (unittest TestCases work in pytest too).
- **Snapshot tests for TUI?** Textual ships a snapshot helper that
  records the rendered frame as SVG. Could be useful for theming /
  layout tests. Defer until a regression motivates it.
- **Real git or faked git?** Real git. Faking git adds complexity
  without much gain — the ops are local, fast, and the actual
  worktree-creation behavior is what we want to verify.
- **Real settings.kdl files or programmatic API?** Real files. The
  parser is the boundary we want to exercise, not an internal
  AppConfig builder.
- **Concurrency tests for the daemon (#35) when it lands?** Yes,
  add a `e2e/tests/daemon/` subtree at that point. The current
  per-actor pattern (`_db()` per call) is the baseline for those
  tests.
