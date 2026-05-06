from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import List

from .errors import GitError
from .interfaces import GitOps


async def _run_git(repo: Path, args: List[str]) -> str:
    """Run a git command in the given directory, returning trimmed stdout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(repo),
        )
    except OSError as e:
        raise GitError(f"failed to run git: {e}")

    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        raise GitError(msg)

    return stdout.decode("utf-8", errors="replace").strip()


class RealGit(GitOps):
    async def create_worktree(self, repo: Path, target: Path, branch: str, base: str) -> None:
        await _run_git(repo, ["worktree", "add", "-b", branch, str(target), base])

    async def remove_worktree(self, repo: Path, target: Path) -> None:
        await _run_git(repo, ["worktree", "remove", str(target), "--force"])

    async def merge_branch(self, repo: Path, branch: str, into: str) -> None:
        original = await _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
        await _run_git(repo, ["checkout", into])
        try:
            await _run_git(repo, ["merge", branch])
        except GitError as e:
            try:
                await _run_git(repo, ["merge", "--abort"])
            except GitError as abort_err:
                print(f"warning: failed to abort merge: {abort_err}", file=sys.stderr)
            try:
                await _run_git(repo, ["checkout", original])
            except GitError as restore_err:
                print(f"warning: failed to restore branch '{original}': {restore_err}", file=sys.stderr)
            raise e
        try:
            await _run_git(repo, ["checkout", original])
        except GitError as restore_err:
            print(f"warning: merge succeeded but failed to restore branch '{original}': {restore_err}", file=sys.stderr)

    async def delete_branch(self, repo: Path, branch: str) -> None:
        await _run_git(repo, ["branch", "-D", branch])

    async def push_branch(self, repo: Path, branch: str) -> None:
        await _run_git(repo, ["push", "-u", "origin", branch])

    async def create_pr(self, repo: Path, branch: str, base: str, title: str, body: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "create",
                "--head", branch,
                "--base", base,
                "--title", title,
                "--body", body,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo),
            )
        except OSError as e:
            raise GitError(f"failed to run gh: {e}")

        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace").strip()
            raise GitError(msg)

        return stdout.decode("utf-8", errors="replace").strip()

    async def current_branch(self, repo: Path) -> str:
        return await _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])

    async def is_repo(self, path: Path) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--git-dir",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(path),
            )
        except OSError:
            return False
        await proc.communicate()
        return proc.returncode == 0
