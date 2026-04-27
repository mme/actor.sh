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
    # Absolute byte offset of this entry's source line in the agent's
    # session rollout file. Populated by the file-based agents so the
    # watch UI (and any other reader) can bucket entries back into the
    # run row that produced them via the run's [log_start_offset,
    # log_end_offset) bracket. None for agents that don't track byte
    # positions (e.g. future in-memory/SQLite-backed agents).
    source_offset: Optional[int] = None


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
    def start(self, dir: Path, prompt: str, config: ActorConfig) -> Tuple[int, Optional[str]]:
        """Start a new agent session. Returns (pid, optional session_id)."""

    @abc.abstractmethod
    def resume(self, dir: Path, session_id: str, prompt: str, config: ActorConfig) -> int:
        """Resume an existing session with a new prompt. Returns pid."""

    @abc.abstractmethod
    def wait(self, pid: int) -> Tuple[int, str]:
        """Wait for the agent process to exit. Returns (exit_code, output)."""

    @abc.abstractmethod
    def read_logs(self, dir: Path, session_id: str) -> List[LogEntry]:
        """Read logs from the agent's session files."""

    def session_file_size(
        self, dir: Path, session_id: str,
    ) -> Optional[int]:
        """Current byte size of the on-disk rollout file for this
        session, or ``None`` if the agent doesn't have a knowable
        file (missing, not yet created, or agent isn't file-based).

        Called by ``cmd_run`` / the interactive manager at run
        boundaries to stamp ``Run.log_start_offset`` (before spawn)
        and ``Run.log_end_offset`` (after exit). Those offsets
        bracket the JSONL region that this run produced, which
        downstream readers use to correlate ``LogEntry.source_offset``
        back to a specific run without relying on timestamp ranges.

        Default is ``None`` — agents without offsets stay on the
        timestamp-range fallback, which is lossy at boundaries but
        doesn't break anything."""
        return None

    def read_logs_since(
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
        return self.read_logs(dir, session_id), None

    @abc.abstractmethod
    def stop(self, pid: int) -> None:
        """Kill a running agent process."""

    @abc.abstractmethod
    def interactive_argv(self, session_id: str, config: ActorConfig) -> List[str]:
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
