"""Helper functions for the watch dashboard."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..types import Actor, Status

# -- Status icons -----------------------------------------------------------

STATUS_ICON = {
    Status.RUNNING: "♤",  # animated in tree.py via RUNNING_FRAMES
    Status.DONE: "",
    Status.ERROR: "󰗖",
    Status.IDLE: "",
    Status.STOPPED: "",
    Status.INTERACTIVE: "⌨",  # U+2328 KEYBOARD — plain, non-emoji variant
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

class FileDiff:
    """Diff for a single file."""
    def __init__(self, file_path: str, old_content: str, new_content: str) -> None:
        self.file_path = file_path
        self.old_content = old_content
        self.new_content = new_content


class DiffResult:
    """Result of compute_diff."""
    def __init__(self, files: list[FileDiff] | None = None, reason: str = "") -> None:
        self.files = files
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
        # Find the merge base — the point where the actor branched off
        merge_base_result = subprocess.run(
            ["git", "merge-base", base_branch, "HEAD"],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        if merge_base_result.returncode == 0 and merge_base_result.stdout.strip():
            diff_ref = merge_base_result.stdout.strip()
        else:
            diff_ref = base_branch

        # Get tracked files that changed
        result = subprocess.run(
            ["git", "diff", "--name-only", diff_ref],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        tracked_files = result.stdout.strip().split("\n") if result.returncode == 0 and result.stdout.strip() else []

        # Get untracked files (new files the actor created)
        untracked_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        untracked_files = untracked_result.stdout.strip().split("\n") if untracked_result.returncode == 0 and untracked_result.stdout.strip() else []

        files = tracked_files + untracked_files
        if not files:
            return DiffResult(reason="working tree clean")

        file_diffs = []
        for f in files:
            orig_result = subprocess.run(
                ["git", "show", f"{diff_ref}:{f}"],
                capture_output=True, text=True, cwd=worktree_dir,
            )
            old_content = orig_result.stdout if orig_result.returncode == 0 else ""

            file_path = Path(worktree_dir) / f
            try:
                new_content = file_path.read_text()
            except (FileNotFoundError, OSError):
                new_content = ""

            if old_content != new_content:
                file_diffs.append(FileDiff(f, old_content, new_content))

        if not file_diffs:
            return DiffResult(reason="working tree clean")

        return DiffResult(files=file_diffs)
    except Exception:
        return DiffResult(reason="error")
