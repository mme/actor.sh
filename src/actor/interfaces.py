from __future__ import annotations

import abc
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .types import ActorConfig


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
    # Token usage for the LLM call this entry came from. Set on at most
    # one LogEntry per JSONL message — typically the first block that
    # produces an entry — so a downstream aggregator can sum it without
    # double-counting. None for message types that don't carry usage
    # info (USER prompts, TOOL_RESULT echoes, etc.).
    usage: Optional[Dict[str, int]] = None


class Agent(abc.ABC):
    # Per-agent defaults. Subclasses fill these in.
    #
    # These are used for two purposes only:
    #   1. Hardcoded baseline defaults merged at actor creation time.
    #   2. Validation whitelist — at the CLI layer, `--config key=value`
    #      whose key appears in ACTOR_DEFAULTS is rejected (actor-keys
    #      have dedicated flags).
    #
    # They are NOT used as a runtime routing table. Once an ActorConfig
    # exists, `cfg.actor_keys` and `cfg.agent_args` carry the split
    # positionally; nothing downstream looks keys up by name.
    #
    # Values are always concrete strings — `None` is a kdl-layer-only
    # cancel marker and never appears in class-level defaults.
    AGENT_DEFAULTS: Dict[str, str] = {}
    ACTOR_DEFAULTS: Dict[str, str] = {}

    @abc.abstractmethod
    def emit_agent_args(self, defaults: Dict[str, str]) -> List[str]:
        """Turn the agent_args dict into CLI flags for the agent binary."""

    @abc.abstractmethod
    def apply_actor_keys(
        self, actor_keys: Dict[str, str], env: Mapping[str, str]
    ) -> Dict[str, str]:
        """Return a NEW env dict with actor-key side effects applied
        (e.g. stripping API keys)."""

    @abc.abstractmethod
    async def start(
        self, dir: Path, prompt: str, config: ActorConfig,
    ) -> Tuple[int, Optional[str]]:
        """Start a new agent session. Returns (pid, optional session_id)."""

    @abc.abstractmethod
    async def resume(
        self, dir: Path, session_id: str, prompt: str, config: ActorConfig,
    ) -> int:
        """Resume an existing session with a new prompt. Returns pid."""

    @abc.abstractmethod
    async def wait(self, pid: int) -> Tuple[int, str]:
        """Wait for the agent process to exit. Returns (exit_code, output)."""

    @abc.abstractmethod
    async def read_logs(self, dir: Path, session_id: str) -> List[LogEntry]:
        """Read logs from the agent's session files."""

    async def read_logs_since(
        self, dir: Path, session_id: str, cursor: Any = None,
    ) -> Tuple[List[LogEntry], Any]:
        """Read entries that have arrived since `cursor`.

        Returns ``(new_entries, next_cursor)``. Pass ``None`` for a full
        read on the first call, then pass back whatever cursor was
        returned from the previous call to pick up from there.

        The cursor is opaque to callers; each agent chooses what to
        return. For the file-based agents (Claude, Codex) it's a byte
        offset into the rollout JSONL; for a hypothetical SQLite-backed
        future agent it could be a row id or timestamp.

        Default implementation falls back to a full read on every call,
        discarding the cursor. Agents that want streaming behavior
        override this; everything else keeps working correctness-wise,
        just without the I/O savings. Watch callers that don't care
        about streaming can ignore the returned cursor."""
        return await self.read_logs(dir, session_id), None

    @abc.abstractmethod
    async def stop(self, pid: int) -> None:
        """Kill a running agent process."""

    @abc.abstractmethod
    def interactive_argv(self, session_id: str, config: ActorConfig) -> List[str]:
        """Argv to launch an interactive session (TTY / PTY). No prompt."""


class GitOps(abc.ABC):
    @abc.abstractmethod
    async def create_worktree(self, repo: Path, target: Path, branch: str, base: str) -> None: ...

    @abc.abstractmethod
    async def remove_worktree(self, repo: Path, target: Path) -> None: ...

    @abc.abstractmethod
    async def merge_branch(self, repo: Path, branch: str, into: str) -> None: ...

    @abc.abstractmethod
    async def delete_branch(self, repo: Path, branch: str) -> None: ...

    @abc.abstractmethod
    async def push_branch(self, repo: Path, branch: str) -> None: ...

    @abc.abstractmethod
    async def create_pr(self, repo: Path, branch: str, base: str, title: str, body: str) -> str: ...

    @abc.abstractmethod
    async def current_branch(self, repo: Path) -> str: ...

    @abc.abstractmethod
    async def is_repo(self, path: Path) -> bool: ...


class ProcessManager(abc.ABC):
    @abc.abstractmethod
    def is_alive(self, pid: int) -> bool: ...

    @abc.abstractmethod
    def kill(self, pid: int) -> None: ...


def binary_exists(name: str) -> bool:
    return shutil.which(name) is not None
