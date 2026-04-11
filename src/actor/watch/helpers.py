"""Helper functions for the watch dashboard."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..types import Actor, Status

# -- Status icons -----------------------------------------------------------

STATUS_ICON = {
    Status.RUNNING: "●",
    Status.DONE: "○",
    Status.ERROR: "✗",
    Status.IDLE: "◌",
    Status.STOPPED: "■",
}


# -- Group actors by parent -------------------------------------------------

def group_by_parent(actors: list[Actor], statuses: dict[str, Status]) -> dict[str | None, list[Actor]]:
    """Group actors by parent, handling cycles and missing parents."""
    actor_names = {a.name for a in actors}

    def _has_cycle(a: Actor) -> bool:
        seen: set[str] = set()
        cur = a.parent
        while cur is not None and cur in actor_names:
            if cur in seen:
                return True
            seen.add(cur)
            parent_actor = next((x for x in actors if x.name == cur), None)
            cur = parent_actor.parent if parent_actor else None
        return False

    by_parent: dict[str | None, list[Actor]] = {}
    for a in actors:
        parent = a.parent if a.parent in actor_names else None
        if parent is not None and _has_cycle(a):
            parent = None
        by_parent.setdefault(parent, []).append(a)

    def sort_key(a: Actor) -> int:
        s = statuses.get(a.name, Status.IDLE)
        order = {Status.RUNNING: 0, Status.ERROR: 1, Status.IDLE: 2, Status.DONE: 3, Status.STOPPED: 4}
        return order.get(s, 9)

    for children in by_parent.values():
        children.sort(key=lambda a: a.updated_at or a.created_at or "", reverse=True)
        children.sort(key=sort_key)

    return by_parent


# -- Read log entries --------------------------------------------------------

def read_log_entries(actor: Actor) -> list:
    """Read raw LogEntry list for an actor."""
    from ..agents.claude import ClaudeAgent
    from ..agents.codex import CodexAgent
    from ..interfaces import Agent
    from ..types import AgentKind

    agent: Agent
    if actor.agent == AgentKind.CLAUDE:
        agent = ClaudeAgent()
    else:
        agent = CodexAgent()

    if actor.agent_session is None:
        return []
    try:
        return agent.read_logs(Path(actor.dir), actor.agent_session)
    except Exception:
        return []


# -- Compute git diff --------------------------------------------------------

class DiffResult:
    """Result of compute_diff."""
    def __init__(self, data: tuple[str, str, str, str] | None = None, reason: str = "") -> None:
        self.data = data
        self.reason = reason


def compute_diff(actor: Actor) -> DiffResult:
    """Compute diff for an actor."""
    worktree_dir = actor.dir

    # Check if the directory is a git repo at all
    check = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True, text=True, cwd=worktree_dir,
    )
    if check.returncode != 0:
        return DiffResult(reason="no repository")

    base_branch = actor.base_branch
    if not base_branch:
        base_branch = "HEAD"

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base_branch],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return DiffResult(reason="no changes")

        files = result.stdout.strip().split("\n")

        orig_parts = []
        mod_parts = []
        for f in files:
            orig_result = subprocess.run(
                ["git", "show", f"{base_branch}:{f}"],
                capture_output=True, text=True, cwd=worktree_dir,
            )
            orig_parts.append(f"# {f}\n" + (orig_result.stdout if orig_result.returncode == 0 else ""))

            file_path = Path(worktree_dir) / f
            try:
                mod_content = file_path.read_text()
            except (FileNotFoundError, OSError):
                mod_content = ""
            mod_parts.append(f"# {f}\n" + mod_content)

        return DiffResult(data=(
            base_branch,
            f"{actor.name} (working tree)",
            "\n".join(orig_parts),
            "\n".join(mod_parts),
        ))
    except Exception:
        return DiffResult(reason="error")
