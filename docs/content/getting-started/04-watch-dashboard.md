---
title: "The watch dashboard"
description: "Tour the Textual TUI for live actor status, logs, diffs, and embedded sessions."
weight: 4
---

`actor watch` opens a live dashboard for everything you have in flight. It's a Textual app — runs in your terminal by default, or in a browser via textual-serve. Most workflows keep it open in one pane while the orchestrator runs in another.

```bash
actor watch
```

## Layout

The dashboard is a master-detail split:

- **Left:** a tree of all actors. Top-level actors are sorted with running first, then by creation time. Children (actors spawned by other actors) are indented under their parent with tree characters.
- **Right:** a tabbed detail panel for whichever actor is selected.
- **Header / footer:** aggregate counts (`4 actors: 2 running, 1 done, 1 error`) and key hints.

### Status icons

Each row in the tree starts with one of:

- `●` running
- `○` done
- `✗` error
- `◌` idle
- `■` stopped

The selected actor is shown with reverse video so it's easy to spot.

### Detail tabs

Four tabs, each switchable by a single letter (case-insensitive, regardless of which pane has focus):

- **L** — Logs. The agent's session output, color-coded by role. Auto-scrolls for running actors. Press `f` to toggle follow mode, `v` to toggle verbose (tool calls, thinking, timestamps).
- **D** — Diff. `git diff` against the actor's base branch, syntax-highlighted. If the actor changed multiple files, a file list appears at the top.
- **R** — Runs. A table of every run for this actor — index, status, exit code, prompt, started-at, duration.
- **I** — Info. Metadata: agent kind, worktree path, source repo, base branch, parent, session ID, created-at, stored config.

When an actor transitions from running to done or error, the dashboard auto-switches you to the Diff tab if you're viewing that actor — so the result of the work lands in front of you without an extra keystroke.

## Navigation

Three navigation schemes work everywhere — pick whichever matches your muscle memory:

| Action          | Vim | Arrow | Emacs    |
| --------------- | --- | ----- | -------- |
| Previous actor  | `k` | up    | `Ctrl+P` |
| Next actor      | `j` | down  | `Ctrl+N` |
| Focus actor list| `h` | left  | —        |
| Focus detail    | `l` | right | —        |

Plus the global keys: `Ctrl+P` opens Textual's built-in command palette (filter by status, jump to actor by name, toggle log verbosity), `/` is search, and `q` quits.

## Interactive sessions

You don't have to leave the dashboard to talk to a sub-actor. Select one in the tree and press **Enter**: the detail pane swaps to an embedded terminal running `claude --resume <session_id>` (or `codex resume <session_id>`) inside the actor's worktree. You're typing directly into a live Claude or Codex session.

A few things to know:

- The actor must not currently be running, and it must already have a session (i.e. it's been run at least once).
- **Ctrl+Z** leaves the embedded widget but keeps the subprocess alive — you can navigate around the dashboard, look at other actors, then come back. Selecting a different actor while a session is parked just shows that actor's logs.
- Quitting watch (`q`) sends SIGTERM to all live subprocesses and marks their runs as `STOPPED`.
- Each interactive session is recorded as a Run with prompt `*interactive*`, so it shows up in the Runs tab and `actor show` alongside normal runs.

## Browser mode

If you'd rather have the dashboard in a browser tab — easier to keep visible alongside other windows, or to share over SSH port-forwarding — pass `--serve`:

```bash
actor watch --serve
```

This starts textual-serve on `localhost:2204` and opens the same Textual app there. Same keys, same layout.

## SSH-friendly mode

The dashboard plays a brief splash animation on startup. Over a slow connection it can stutter; disable it with:

```bash
actor watch --no-animation
```

## Diagnostics

If an embedded interactive session does something weird — keys not registering, ANSI sequences mis-rendering, the widget appearing to hang — press **Ctrl+Shift+D** to dump the I/O ring buffer for the active session to stderr. The buffer holds the recent stream of bytes read from and written to the PTY plus the events the widget handled, which is usually enough to figure out where the loop went sideways. Useful when filing a bug.
