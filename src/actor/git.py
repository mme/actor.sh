from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

from .errors import GitError
from .interfaces import GitOps


def _run_git(repo: Path, args: List[str]) -> str:
    """Run a git command in the given directory, returning trimmed stdout."""
    try:
        result = subprocess.run(
            ["git"] + args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            cwd=str(repo),
        )
    except OSError as e:
        raise GitError(f"failed to run git: {e}")

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(stderr)

    return result.stdout.decode("utf-8", errors="replace").strip()


class RealGit(GitOps):
    def create_worktree(self, repo: Path, target: Path, branch: str, base: str) -> None:
        _run_git(repo, ["worktree", "add", "-b", branch, str(target), base])

    def remove_worktree(self, repo: Path, target: Path) -> None:
        _run_git(repo, ["worktree", "remove", str(target), "--force"])

    def merge_branch(self, repo: Path, branch: str, into: str) -> None:
        original = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
        _run_git(repo, ["checkout", into])
        try:
            _run_git(repo, ["merge", branch])
        except GitError as e:
            try:
                _run_git(repo, ["merge", "--abort"])
            except GitError as abort_err:
                print(f"warning: failed to abort merge: {abort_err}", file=sys.stderr)
            try:
                _run_git(repo, ["checkout", original])
            except GitError as restore_err:
                print(f"warning: failed to restore branch '{original}': {restore_err}", file=sys.stderr)
            raise e
        try:
            _run_git(repo, ["checkout", original])
        except GitError as restore_err:
            print(f"warning: merge succeeded but failed to restore branch '{original}': {restore_err}", file=sys.stderr)

    def delete_branch(self, repo: Path, branch: str) -> None:
        _run_git(repo, ["branch", "-D", branch])

    def push_branch(self, repo: Path, branch: str) -> None:
        _run_git(repo, ["push", "-u", "origin", branch])

    def create_pr(self, repo: Path, branch: str, base: str, title: str, body: str) -> str:
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--head", branch,
                    "--base", base,
                    "--title", title,
                    "--body", body,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                cwd=str(repo),
            )
        except OSError as e:
            raise GitError(f"failed to run gh: {e}")

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(stderr)

        return result.stdout.decode("utf-8", errors="replace").strip()

    def current_branch(self, repo: Path) -> str:
        return _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])

    def is_repo(self, path: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                cwd=str(path),
            )
            return result.returncode == 0
        except OSError:
            return False
