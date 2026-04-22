"""Lifecycle hook execution for actor.sh.

Hooks are single shell commands declared in settings.kdl under the
`hooks {}` block. They run via /bin/sh -c, inherit the caller's env plus
ACTOR_* variables, and their exit code decides whether the surrounding
operation (create / run / discard) proceeds.

Kept as a separate module so tests can inject a fake runner via the
HookRunner type without touching subprocess.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, MutableMapping, Optional, Union

from .errors import HookFailedError


def merge_env_extra(
    env: MutableMapping[str, str],
    env_extra: Optional[Mapping[str, Optional[str]]],
) -> None:
    """Merge ``env_extra`` into ``env`` in place. A value of ``None`` means
    "unset this key" so callers can scrub stale parent-env vars without
    mutating ``os.environ`` (thread-safe for concurrent Agent calls)."""
    if env_extra is None:
        return
    for key, value in env_extra.items():
        if value is None:
            env.pop(key, None)
        elif isinstance(value, str) and not isinstance(value, bool):
            env[key] = value
        else:
            raise TypeError(
                f"env_extra[{key!r}] must be str or None, got "
                f"{type(value).__name__} ({value!r})"
            )


@dataclass(frozen=True)
class HookResult:
    """Outcome of a single hook invocation.

    The default runner captures stdio so inherited stdout/stderr can't
    corrupt the parent (MCP server's JSON, TUI redraws, etc.).
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""


# Signature: (command, env, cwd) -> int | HookResult. Test fakes
# typically return a bare int; the default runner returns HookResult so
# captured stdio can flow into HookFailedError on failure. ``run_hook``
# normalizes both.
HookRunner = Callable[[str, Mapping[str, str], Path], Union[int, HookResult]]


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
    result = exec_runner(command, env, cwd)
    if isinstance(result, HookResult):
        exit_code = result.exit_code
        stdout = result.stdout
        stderr = result.stderr
    elif isinstance(result, int) and not isinstance(result, bool):
        exit_code = result
        stdout = ""
        stderr = ""
    else:
        raise TypeError(
            f"HookRunner for '{event}' returned {type(result).__name__} "
            f"({result!r}); expected int or HookResult"
        )
    if exit_code != 0:
        raise HookFailedError(
            event, command, exit_code, stdout=stdout, stderr=stderr,
        )


def _default_hook_runner(
    command: str, env: Mapping[str, str], cwd: Path
) -> HookResult:
    proc = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return HookResult(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def hook_env(
    base_env: Mapping[str, str],
    actor_name: str,
    actor_dir: Path,
    actor_agent: str,
    actor_session_id: Optional[str],
    actor_run_id: Optional[int] = None,
    actor_exit_code: Optional[int] = None,
    actor_duration_ms: Optional[int] = None,
) -> Dict[str, str]:
    """Return a fresh dict combining base_env with ACTOR_* variables.

    ``actor_run_id``, ``actor_exit_code``, and ``actor_duration_ms`` are
    run-specific and only populated for hooks that fire around a
    completed run (currently only ``after-run``)."""
    env = dict(base_env)
    env["ACTOR_NAME"] = actor_name
    env["ACTOR_DIR"] = str(actor_dir)
    env["ACTOR_AGENT"] = actor_agent
    if actor_session_id is not None:
        env["ACTOR_SESSION_ID"] = actor_session_id
    else:
        env.pop("ACTOR_SESSION_ID", None)
    for key, value in (
        ("ACTOR_RUN_ID", actor_run_id),
        ("ACTOR_EXIT_CODE", actor_exit_code),
        ("ACTOR_DURATION_MS", actor_duration_ms),
    ):
        if value is None:
            env.pop(key, None)
            continue
        if isinstance(value, bool):
            raise TypeError(
                f"{key} expects int, got bool ({value!r})"
            )
        env[key] = str(value)
    return env
