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

    Set `input` to feed stdin (e.g. for `actor new foo` reading a
    piped prompt). Defaults to no stdin (subprocess.DEVNULL).
    """
    cmd = [_resolve_actor_bin(), *args]
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd) if cwd else None,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        # input=None becomes empty stdin; explicit DEVNULL keeps
        # behavior consistent across Python versions.
        stdin=subprocess.DEVNULL if input is None else None,
    )
