from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("actor-sh")
except PackageNotFoundError:
    __version__ = "unknown"

# Errors
from .errors import (
    ActorError,
    AlreadyExistsError,
    NotFoundError,
    IsRunningError,
    NotRunningError,
    InvalidNameError,
    AgentNotFoundError,
    GitError,
    ConfigError,
    HookFailedError,
)

# Types
from .types import (
    AgentKind,
    Status,
    Actor,
    Run,
    ActorConfig,
    validate_name,
    parse_config,
    _now_iso,
    _parse_iso,
    _sorted_config,
)

# Config
from .config import AgentDefaults, AppConfig, Hooks, Role, load_config

# Hooks runtime
from .hooks import HookResult, HookRunner, hook_env, run_hook

# Interfaces
from .interfaces import (
    LogEntryKind,
    LogEntry,
    Agent,
    GitOps,
    ProcessManager,
    binary_exists,
)

# Database
from .db import Database

# Agents
from .agents.claude import ClaudeAgent
from .agents.codex import CodexAgent

# Git
from .git import RealGit, _run_git

# Process
from .process import RealProcessManager

# Service
from .service import (
    ActorService,
    LocalActorService,
    Notification,
    INTERACTIVE_PROMPT,
    RunStartResult,
    RunResult,
    StopResult,
    DiscardResult,
    ActorDetail,
    InteractiveRunHandle,
    LogsResult,
    agent_class,
    create_agent,
)

# Display helpers (also available for tests)
from .cli_format import (
    truncate,
    format_duration,
    worktree_path,
    encode_dir,
    claude_session_file_path,
    claude_read_logs,
)

# CLI
from .cli import main, _build_parser, _db_path, _create_agent

__all__ = [
    # Version
    "__version__",
    # Errors
    "ActorError",
    "AlreadyExistsError",
    "NotFoundError",
    "IsRunningError",
    "NotRunningError",
    "InvalidNameError",
    "AgentNotFoundError",
    "GitError",
    "ConfigError",
    "HookFailedError",
    # Types
    "AgentKind",
    "Status",
    "Actor",
    "Run",
    "ActorConfig",
    "validate_name",
    "parse_config",
    # Config
    "AgentDefaults",
    "AppConfig",
    "Hooks",
    "Role",
    "load_config",
    # Hooks runtime
    "HookResult",
    "HookRunner",
    "hook_env",
    "run_hook",
    # Interfaces
    "LogEntryKind",
    "LogEntry",
    "Agent",
    "GitOps",
    "ProcessManager",
    "binary_exists",
    # Database
    "Database",
    # Agents
    "ClaudeAgent",
    "CodexAgent",
    # Git
    "RealGit",
    # Process
    "RealProcessManager",
    # Service
    "ActorService",
    "LocalActorService",
    "Notification",
    "INTERACTIVE_PROMPT",
    "RunStartResult",
    "RunResult",
    "StopResult",
    "DiscardResult",
    "ActorDetail",
    "InteractiveRunHandle",
    "LogsResult",
    "agent_class",
    "create_agent",
    # Helpers
    "truncate",
    "format_duration",
    "worktree_path",
    "encode_dir",
    "claude_session_file_path",
    "claude_read_logs",
    # CLI
    "main",
]
