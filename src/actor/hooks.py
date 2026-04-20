"""Lifecycle hook execution for actor.sh (issue #30).

Hooks are single shell commands declared in settings.kdl under the
`hooks {}` block. They run via /bin/sh -c, inherit the caller's env plus
ACTOR_* variables, and their exit code decides whether the surrounding
operation (create / run / discard) proceeds.

Kept as a tiny module so tests can inject a fake runner via the
HookRunner type without touching subprocess.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

from .errors import HookFailedError

# Signature: (command, env, cwd) -> exit_code
HookRunner = Callable[[str, Mapping[str, str], Path], int]


def run_hook(
    event: str,
    command: Optional[str],
    env: Mapping[str, str],
    cwd: Path,
    runner: Optional[HookRunner] = None,
) -> None:
    """Execute a hook. No-op when command is None. Raises HookFailedError
    on non-zero exit."""
    if command is None:
        return
    exec_runner = runner if runner is not None else _default_hook_runner
    exit_code = exec_runner(command, env, cwd)
    if exit_code != 0:
        raise HookFailedError(event, command, exit_code)


def _default_hook_runner(
    command: str, env: Mapping[str, str], cwd: Path
) -> int:
    proc = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=str(cwd),
        env=dict(env),
    )
    return proc.returncode


def hook_env(
    base_env: Mapping[str, str],
    actor_name: str,
    actor_dir: Path,
    actor_agent: str,
    actor_session_id: Optional[str],
) -> dict:
    """Return a fresh dict combining base_env with ACTOR_* variables."""
    env = dict(base_env)
    env["ACTOR_NAME"] = actor_name
    env["ACTOR_DIR"] = str(actor_dir)
    env["ACTOR_AGENT"] = actor_agent
    if actor_session_id is not None:
        env["ACTOR_SESSION_ID"] = actor_session_id
    return env
