"""Interactive embedded-terminal support for `actor watch`.

Pure modules (unit-testable, no I/O):
  screen.py       — pyte wrapper + rich.Text rendering
  input.py        — key + mouse event → bytes translator
  batcher.py      — refresh coalescer (flicker prevention)
  diagnostics.py  — ring buffer recording I/O events for post-mortem

Impure modules (integration-tested):
  pty_session.py  — pty.fork + read/write/resize/close lifecycle
  widget.py       — Textual widget gluing the pure parts to a live session
  manager.py      — dict[actor_name, PtySession], lifecycle on the app
"""
