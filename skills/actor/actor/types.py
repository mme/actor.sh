from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from .errors import ActorError, ConfigError, InvalidNameError


class AgentKind(Enum):
    CLAUDE = "claude"
    CODEX = "codex"

    @property
    def binary_name(self) -> str:
        return self.value

    def as_str(self) -> str:
        return self.value

    @classmethod
    def from_str(cls, s: str) -> AgentKind:
        for member in cls:
            if member.value == s:
                return member
        raise ActorError(f"unknown agent: {s}")

    def __str__(self) -> str:
        return self.value


class Status(Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    STOPPED = "stopped"

    def as_str(self) -> str:
        return self.value

    @classmethod
    def from_str(cls, s: str) -> Status:
        for member in cls:
            if member.value == s:
                return member
        raise ActorError(f"unknown status: {s}")

    def __str__(self) -> str:
        return self.value


# Config is a sorted dict (matching Rust BTreeMap)
Config = Dict[str, str]


def _sorted_config(d: Dict[str, str]) -> Dict[str, str]:
    """Return a dict sorted by key (matching BTreeMap behavior)."""
    return dict(sorted(d.items()))


@dataclass
class Actor:
    name: str
    agent: AgentKind
    agent_session: Optional[str]
    dir: str
    source_repo: Optional[str]
    base_branch: Optional[str]
    worktree: bool
    config: Config
    created_at: str
    updated_at: str


@dataclass
class Run:
    id: int
    actor_name: str
    prompt: str
    status: Status
    exit_code: Optional[int]
    pid: Optional[int]
    config: Config
    started_at: str
    finished_at: Optional[str]


def validate_name(name: str) -> None:
    """Validate an actor name, raising InvalidNameError on failure."""
    if not name:
        raise InvalidNameError("actor name cannot be empty")
    if len(name) > 64:
        raise InvalidNameError("actor name cannot exceed 64 characters")
    first = name[0]
    if not first.isascii() or not first.isalnum():
        raise InvalidNameError("actor name must start with a letter or number")
    if not all(c.isascii() and (c.isalnum() or c in "._-") for c in name):
        raise InvalidNameError("actor name can only contain [a-zA-Z0-9._-]")
    reserved = ["main", "master", "HEAD"]
    if name in reserved:
        raise InvalidNameError(f"'{name}' is a reserved name")


def parse_config(pairs: List[str]) -> Config:
    """Parse key=value config pairs into a dict."""
    config: Config = {}
    for pair in pairs:
        idx = pair.find("=")
        if idx == -1:
            raise ConfigError(f"invalid config pair: {pair}")
        key = pair[:idx]
        value = pair[idx + 1:]
        config[key] = value
    return _sorted_config(config)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO 8601 / RFC 3339 timestamp (3.9-compatible)."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
