"""Implementation for 'actor setup' and 'actor update'.

'setup' deploys the bundled Claude Code skill + registers the MCP server
with the target coding agent host. 'update' re-runs just the skill-copy
step to pick up a newer version of actor-sh without touching the MCP
registration.

Only --for claude-code is supported today. Other hosts error cleanly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from importlib.resources import as_file, files
from pathlib import Path
from typing import Iterable

from . import __version__
from .errors import ActorError

SUPPORTED_HOSTS = ("claude-code",)
SUPPORTED_SCOPES = ("user", "project", "local")


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


def _skill_target_dir(host: str, scope: str, name: str) -> Path:
    """Where the deployed skill directory should live for this host + scope."""
    _validate_host(host)
    # claude-code uses ~/.claude/skills/<name>/ for user/local, .claude/skills/<name>/ for project
    if scope == "project":
        base = Path.cwd() / ".claude" / "skills"
    else:  # user, local
        home = os.environ.get("HOME", "")
        if not home:
            raise ActorError("HOME environment variable is not set")
        base = Path(home) / ".claude" / "skills"
    return base / name


def _copy_bundled_skill(target: Path) -> list[str]:
    """Copy the bundled src/actor/_skill contents to target. Returns list of file names copied."""
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    source = files("actor._skill")
    for entry in source.iterdir():
        name = entry.name
        # Skip Python-only artifacts
        if name.startswith("__") or name.endswith(".py"):
            continue
        with as_file(entry) as entry_path:
            shutil.copy(entry_path, target / name)
        copied.append(name)
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


def _claude_mcp_add(name: str, scope: str, for_host: str) -> None:
    """Register the MCP server with Claude Code via its CLI."""
    cmd = [
        "claude", "mcp", "add", name,
        "--scope", scope,
        "--", "actor", "mcp", "--for", for_host,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise ActorError(
            "`claude` CLI not found on PATH. Install Claude Code first "
            "(https://claude.com/claude-code), then re-run 'actor setup'."
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

    target = _skill_target_dir(for_host, scope, name)
    if target.exists() and not force:
        raise ActorError(
            f"{target} already exists. Use --force to overwrite, "
            "or run 'actor update' to refresh an existing install."
        )
    if target.exists():
        shutil.rmtree(target)

    copied = _copy_bundled_skill(target)
    _stamp_version(target / "SKILL.md", __version__)
    _claude_mcp_add(name=name, scope=scope, for_host=for_host)

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

    target = _skill_target_dir(for_host, scope, name)
    if not target.exists() or not (target / "SKILL.md").exists():
        raise ActorError(
            f"No actor skill found at {target}. "
            f"Run 'actor setup --for {for_host} --scope {scope}' first."
        )

    before = (target / "SKILL.md").read_text()
    _copy_bundled_skill(target)
    new_version = __version__
    _stamp_version(target / "SKILL.md", new_version)

    # Crude version-delta detection: compare frontmatter version strings
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
