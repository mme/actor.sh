from __future__ import annotations

import abc
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from .types import Config


class LogEntryKind(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


@dataclass
class LogEntry:
    kind: LogEntryKind
    timestamp: Optional[str] = None
    text: str = ""
    name: str = ""        # for TOOL_USE
    input: str = ""       # for TOOL_USE
    content: str = ""     # for TOOL_RESULT


class Agent(abc.ABC):
    @abc.abstractmethod
    def start(self, dir: Path, prompt: str, config: Config) -> Tuple[int, Optional[str]]:
        """Start a new agent session. Returns (pid, optional session_id)."""

    @abc.abstractmethod
    def resume(self, dir: Path, session_id: str, prompt: str, config: Config) -> int:
        """Resume an existing session with a new prompt. Returns pid."""

    @abc.abstractmethod
    def wait(self, pid: int) -> Tuple[int, str]:
        """Wait for the agent process to exit. Returns (exit_code, output)."""

    @abc.abstractmethod
    def read_logs(self, dir: Path, session_id: str) -> List[LogEntry]:
        """Read logs from the agent's session files."""

    @abc.abstractmethod
    def stop(self, pid: int) -> None:
        """Kill a running agent process."""

    @abc.abstractmethod
    def interactive_argv(self, session_id: str, config: Config) -> List[str]:
        """Argv to launch an interactive session (TTY / PTY). No prompt."""


class GitOps(abc.ABC):
    @abc.abstractmethod
    def create_worktree(self, repo: Path, target: Path, branch: str, base: str) -> None: ...

    @abc.abstractmethod
    def remove_worktree(self, repo: Path, target: Path) -> None: ...

    @abc.abstractmethod
    def merge_branch(self, repo: Path, branch: str, into: str) -> None: ...

    @abc.abstractmethod
    def delete_branch(self, repo: Path, branch: str) -> None: ...

    @abc.abstractmethod
    def push_branch(self, repo: Path, branch: str) -> None: ...

    @abc.abstractmethod
    def create_pr(self, repo: Path, branch: str, base: str, title: str, body: str) -> str: ...

    @abc.abstractmethod
    def current_branch(self, repo: Path) -> str: ...

    @abc.abstractmethod
    def is_repo(self, path: Path) -> bool: ...


class ProcessManager(abc.ABC):
    @abc.abstractmethod
    def is_alive(self, pid: int) -> bool: ...

    @abc.abstractmethod
    def kill(self, pid: int) -> None: ...


def binary_exists(name: str) -> bool:
    return shutil.which(name) is not None
