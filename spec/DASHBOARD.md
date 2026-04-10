# actor watch — Dashboard Spec

## Overview

`actor watch` opens a Textual app served in the browser via textual-serve on `localhost:2204`. It provides a read-only, real-time view of all actors and their state. Also runs standalone in a terminal.

## Layout

Master-detail split:

```
┌─────────────────────────┬──────────────────────────────────────────────┐
│ ACTORS                  │ [L]ogs  [D]iff  [R]uns  [I]nfo             │
│                         │                                             │
│ ● fix-auth      running │ USER: Fix the auth module token validation  │
│ ● fix-nav       running │                                             │
│   ├─ fix-nav-v2 done    │ ASSISTANT: I'll refactor the token          │
│   └─ fix-nav-v3 running │ validation logic. First, let me examine     │
│ ○ update-tests  done    │ the current implementation...               │
│ ✗ rewrite-api   error   │                                             │
│                         │ ASSISTANT: Done. Simplified the token        │
│                         │ validation from 3 functions to 1. All        │
│                         │ tests pass.                                  │
│                         │                                             │
│                         │                                             │
│                         │                                             │
├─────────────────────────┴──────────────────────────────────────────────┤
│ 4 actors: 2 running, 1 done, 1 error              localhost:2204      │
└───────────────────────────────────────────────────────────────────────┘
```

### Actor list (left panel, ~24 cols)

- Each actor shows: status icon, name, status text
- **Parent-child tree**: children are indented with tree characters (`├─`, `└─`)
- Top-level actors sorted: running first, then by creation time
- Status icons: `●` running, `○` done, `✗` error, `◌` idle, `■` stopped
- Selected actor highlighted with reverse video

### Detail panel (right)

Tabbed view. Tabs selected by single letter:
- **[L]ogs** — agent session output (default tab)
- **[D]iff** — git diff of the actor's branch
- **[R]uns** — run history table
- **[I]nfo** — actor metadata, config, worktree path, session ID

### Header

Aggregate status counts: `4 actors: 2 running, 1 done, 1 error`

### Footer

Key hints: `↑↓ navigate  L/D/R/I tabs  Ctrl+P command palette  q quit`

## Navigation

All three schemes work everywhere:

| Action | Vim | Arrow | Emacs |
|---|---|---|---|
| Previous actor | `k` | `↑` | `Ctrl+P` |
| Next actor | `j` | `↓` | `Ctrl+N` |
| Focus list | `h` | `←` | — |
| Focus detail | `l` | `→` | — |

Tab switching via single letters (case-insensitive, work regardless of focus):
- `L` — Logs
- `D` — Diff
- `R` — Runs
- `I` — Info

Other:
- `Ctrl+P` — command palette (Textual built-in)
- `q` — quit
- `/` — search/filter actors (via command palette)

## Tabs

### Logs

Displays the Claude JSONL session output, color-coded:
- `USER:` prompts in one color
- `ASSISTANT:` responses in another
- Tool calls collapsed by default, expandable
- Auto-scrolls to bottom for running actors (follow mode)
- `f` toggles follow mode
- `v` toggles verbose (show tool calls, thinking, timestamps)

### Diff

Uses [textual-diff-view](https://github.com/batrachianai/textual-diff-view).

- Computes `git diff <base_branch>...<actor_branch>` for the actor's worktree
- Split and unified view modes, toggled with `s`
- File list at top if multiple files changed, selectable
- Refreshes on tab switch (debounced)

### Runs

Table of all runs for the selected actor:

```
#  STATUS  EXIT  PROMPT                          STARTED      DURATION
3  done    0     Fix the token validation...     2m ago       45s
2  error   1     Refactor the auth module...     10m ago      2m 12s
1  done    0     Set up initial structure...     1h ago       1m 30s
```

### Info

Actor metadata in a simple key-value layout:

```
Name:        fix-auth
Agent:       claude
Status:      running
Dir:         ~/.actor/worktrees/fix-auth
Source:      /Users/mme/Projects/myapp
Base:        main
Parent:      —
Session:     a1b2c3d4-...
Created:     2026-04-10 14:30:00
Config:      model=opus
```

## Auto-behavior

- When an actor finishes, show a toast notification: `✓ fix-auth done` or `✗ rewrite-api error`
- When an actor transitions to done/error, auto-switch to Diff tab if the user is viewing that actor
- The selected actor stays selected even as the list reorders

## Command palette

Textual's built-in command palette (`Ctrl+P`) with commands:
- Filter actors by status
- Jump to actor by name
- Toggle log verbosity
- Switch diff view mode

## Data flow

- **Actor list**: 2-second SQLite polling in a worker thread. Each poll reads all actors, resolves status via PID checks, diffs against previous state to detect changes.
- **Logs**: For the selected actor, 1-second polling via incremental byte offset reads of the JSONL session file.
- **Diff**: Computed on-demand when the Diff tab is shown or the actor changes. Runs `git diff` in a worker thread. Debounced at 5 seconds.
- **Runs**: Queried from DB when the Runs tab is shown or actor changes.
- **All DB access**: Read-only, WAL mode, separate connection per worker thread.

## Technical

- Built entirely with Textual
- Served via textual-serve on `localhost:2204`
- Command: `actor watch`
- Also runs standalone in terminal: `actor watch --no-serve`
- Dependencies: `textual`, `textual-serve`, `textual-diff-view`

## Not in scope

- Write operations (stop, discard, run actors from dashboard)
- Embedded terminal
- Graceful degradation for missing widgets
- Custom themes
