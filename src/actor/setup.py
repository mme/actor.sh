"""'setup' deploys the bundled skill + registers the MCP server with a host.
'update' re-runs the skill-copy step to pick up a new actor-sh version
without touching the MCP registration.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import as_file, files
from pathlib import Path

from . import __version__
from .errors import ActorError

SUPPORTED_HOSTS = ("claude-code",)
SUPPORTED_SCOPES = ("user", "project", "local")
_CLAUDE_MCP_TIMEOUT_SEC = 30
# Skill name is used as a filesystem path segment; reject anything that
# could escape the target parent dir or produce nonsense paths.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_host(host: str) -> None:
    if host not in SUPPORTED_HOSTS:
        raise ActorError(
            f"--for {host!r} is not supported yet. "
            f"Supported: {', '.join(SUPPORTED_HOSTS)}."
        )


def _validate_scope(scope: str) -> None:
    if scope not in SUPPORTED_SCOPES:
        raise ActorError(
            f"--scope {scope!r} is invalid. "
            f"Supported: {', '.join(SUPPORTED_SCOPES)}."
        )


def _validate_name(name: str) -> None:
    if not _SAFE_NAME_RE.match(name):
        raise ActorError(
            f"invalid --name {name!r}: must start with an alphanumeric and "
            "contain only [A-Za-z0-9._-]"
        )


def _skill_target_dir(host: str, scope: str, name: str) -> Path:
    """Path the deployed skill should live at. May or may not exist on disk."""
    _validate_host(host)
    _validate_name(name)
    if scope == "project":
        base = Path.cwd() / ".claude" / "skills"
    else:
        # local scope only changes where claude mcp stores the registration;
        # the skill files themselves always live under the user's home.
        home = os.environ.get("HOME", "")
        if not home:
            raise ActorError("HOME environment variable is not set")
        base = Path(home) / ".claude" / "skills"
    return base / name


def _copy_bundled_skill(target: Path) -> list[str]:
    """Copy the bundled src/actor/_skill contents to target. Returns list of file names copied.

    Raises ActorError if the bundled resources don't include a SKILL.md (indicates
    a broken install where the package data didn't ship).
    """
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    source = files("actor._skill")
    for entry in source.iterdir():
        name = entry.name
        # __init__.py and __pycache__ are package plumbing, not skill content
        if name.startswith("__") or name.endswith(".py"):
            continue
        # Skill subdirectories aren't used today; skip rather than crash on
        # shutil.copy so a future nested layout fails gracefully here.
        if entry.is_dir():
            continue
        with as_file(entry) as entry_path:
            shutil.copy(entry_path, target / name)
        copied.append(name)
    if "SKILL.md" not in copied:
        raise ActorError(
            "bundled skill resources are missing SKILL.md — actor-sh may be "
            "installed from an incomplete source tree. Try "
            "`uv tool install --force actor-sh` (or re-install from the wheel)."
        )
    return copied


def _stamp_version(skill_md: Path, version: str) -> None:
    """Write `version: <value>` into the SKILL.md YAML frontmatter.

    If a version line already exists, replace it. Otherwise insert it just
    above the closing '---' of the frontmatter block.
    """
    text = skill_md.read_text()
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].rstrip() == "---":
        raise ActorError(f"{skill_md} has no YAML frontmatter")

    # Find the closing '---'
    close_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            close_idx = i
            break
    if close_idx is None:
        raise ActorError(f"{skill_md} has no closing '---' in frontmatter")

    # Find an existing 'version:' line within the frontmatter
    version_line = f"version: {version}\n"
    for i in range(1, close_idx):
        if lines[i].startswith("version:"):
            lines[i] = version_line
            skill_md.write_text("".join(lines))
            return
    # Not present — insert just before close
    lines.insert(close_idx, version_line)
    skill_md.write_text("".join(lines))


def _run_claude(*args: str, timeout: int = _CLAUDE_MCP_TIMEOUT_SEC) -> subprocess.CompletedProcess[str]:
    """Run `claude ...` with a timeout, surfacing a clear error if the CLI is missing or hangs."""
    try:
        return subprocess.run(
            ["claude", *args], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise ActorError(
            "`claude` CLI not found on PATH. Install Claude Code first "
            "(https://claude.com/claude-code), then re-run 'actor setup'."
        )
    except subprocess.TimeoutExpired:
        raise ActorError(
            f"`claude {' '.join(args)}` timed out after {timeout}s. "
            "Try running it manually to see what's happening."
        )
    except OSError as e:
        # Catches PermissionError, broken-pipe-style OSError, and friends.
        raise ActorError(
            f"could not invoke `claude {' '.join(args)}`: {e}. "
            "Check that the claude CLI is executable."
        )


def _claude_mcp_remove(name: str, scope: str) -> None:
    """Best-effort remove an existing MCP registration. Prints to stderr on success
    (so --force clobbering a real entry is visible); non-zero exit means no entry
    existed, which is fine."""
    result = _run_claude("mcp", "remove", name, "--scope", scope)
    if result.returncode == 0:
        print(
            f"[setup] removed existing MCP registration for '{name}' (scope={scope})",
            file=sys.stderr,
        )


def _claude_mcp_add(name: str, scope: str, for_host: str) -> None:
    """Register the MCP server with Claude Code via its CLI."""
    result = _run_claude(
        "mcp", "add", name,
        "--scope", scope,
        "--", "actor", "mcp", "--for", for_host,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ActorError(f"`claude mcp add` failed: {stderr}")


def cmd_setup(
    *,
    for_host: str,
    scope: str,
    name: str,
    force: bool,
) -> str:
    _validate_host(for_host)
    _validate_scope(scope)
    _validate_name(name)

    target = _skill_target_dir(for_host, scope, name)
    if target.exists() and not force:
        raise ActorError(
            f"{target} already exists. Use --force to overwrite, "
            "or run 'actor update' to refresh an existing install."
        )

    # Stage the new skill in a sibling temp dir, validate (SKILL.md present,
    # version stampable), then atomically swap. This way a broken install or
    # a missing bundled resource doesn't destroy the existing skill.
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=parent, prefix=f".{name}-staging-"))
    old_backup: Path | None = None
    try:
        _copy_bundled_skill(staging)
        _stamp_version(staging / "SKILL.md", __version__)

        if target.exists():
            old_backup = parent / f".{name}-old-{os.getpid()}"
            target.rename(old_backup)
        staging.rename(target)
    except Exception:
        # Roll back the swap if we'd already moved the old install aside
        if old_backup is not None and old_backup.exists() and not target.exists():
            old_backup.rename(target)
            old_backup = None
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # Swap succeeded — clean up backup and stale MCP registration
    if old_backup is not None:
        shutil.rmtree(old_backup, ignore_errors=True)

    # From here on the skill is deployed. Wrap MCP steps so a failure tells
    # the user how to retry just the registration without wiping state.
    try:
        if old_backup is not None:
            _claude_mcp_remove(name=name, scope=scope)
        _claude_mcp_add(name=name, scope=scope, for_host=for_host)
    except ActorError as e:
        raise ActorError(
            f"skill deployed to {target}, but MCP registration failed: {e}. "
            "Re-run `actor setup --for ... --force` to retry."
        )

    return (
        f"actor skill installed at {target} and MCP server registered "
        f"(scope={scope}, name={name}, for={for_host}). "
        f"Restart your Claude Code session to pick up the new skill and tools."
    )


def cmd_update(
    *,
    for_host: str,
    scope: str,
    name: str,
) -> str:
    _validate_host(for_host)
    _validate_scope(scope)
    _validate_name(name)

    new_version = __version__
    if new_version == "unknown":
        raise ActorError(
            "cannot determine installed actor-sh version — reinstall with "
            "`uv tool install --force actor-sh` (or `pip install --upgrade actor-sh`), "
            "then re-run `actor update`."
        )

    target = _skill_target_dir(for_host, scope, name)
    if not target.exists() or not (target / "SKILL.md").exists():
        raise ActorError(
            f"No actor skill found at {target}. "
            f"Run 'actor setup --for {for_host} --scope {scope}' first."
        )

    before = (target / "SKILL.md").read_text()
    _copy_bundled_skill(target)
    _stamp_version(target / "SKILL.md", new_version)

    prev_version = _parse_frontmatter_version(before) or "unknown"
    if prev_version == new_version:
        return f"actor skill at {target} is already at version {new_version}."
    return (
        f"actor skill at {target} updated from {prev_version} to {new_version}. "
        "Restart your Claude Code session to pick up the changes."
    )


def _parse_frontmatter_version(text: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    for line in lines[1:]:
        if line.rstrip() == "---":
            return None
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None
