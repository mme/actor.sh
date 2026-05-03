"""Subprocess wrapper around the `actor` CLI."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _resolve_actor_bin() -> str:
    """Locate the `actor` CLI. Prefer the one on PATH (most common in
    CI / dev), otherwise fall back to invoking the package via
    `python -m actor.cli`. Returns a string suitable for the first
    element of subprocess args."""
    on_path = shutil.which("actor")
    if on_path:
        return on_path
    # Fallback: tests that don't have `uv tool install -e .` done can
    # still run via `python -m`.
    return "actor"


def run_actor_cli(
    args: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[Path] = None,
    input: Optional[str] = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """Invoke `actor <args...>`. Captures stdout, stderr, exit code.

    When `input` is None, the subprocess's stdin is a real PTY (no
    data) — this mimics a user typing in a terminal, which is what
    actor's CLI checks via `sys.stdin.isatty()` before deciding to
    consume stdin as a prompt. Without a TTY, every `actor new
    <name>` (no positional prompt) would error with "stdin was
    empty — expected a prompt", which is a subprocess artifact, not
    a real CLI behavior the user would hit.

    When `input` is a string, stdin is a normal pipe and the data
    is fed in — matches `echo prompt | actor new <name>`.
    """
    cmd = [_resolve_actor_bin(), *args]
    if input is None:
        master_fd, slave_fd = _openpty_pair()
        try:
            r = subprocess.run(
                cmd,
                env=env,
                cwd=str(cwd) if cwd else None,
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                close_fds=True,
            )
            return r
        finally:
            import os
            try:
                os.close(slave_fd)
            except OSError:
                pass
            try:
                os.close(master_fd)
            except OSError:
                pass
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd) if cwd else None,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _openpty_pair() -> tuple[int, int]:
    """Open a pty pair. Returns (master_fd, slave_fd). Caller closes."""
    import pty
    return pty.openpty()
