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
)

# Types
from .types import (
    AgentKind,
    Status,
    Actor,
    Run,
    Config,
    validate_name,
    parse_config,
    _now_iso,
    _parse_iso,
    _sorted_config,
)

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

# Commands & helpers
from .commands import (
    cmd_new,
    cmd_run,
    cmd_interactive,
    INTERACTIVE_PROMPT,
    cmd_list,
    cmd_show,
    cmd_stop,
    cmd_config,
    cmd_logs,
    cmd_discard,
    truncate,
    format_duration,
    worktree_path,
    encode_dir,
    claude_session_file_path,
    claude_config_args,
    codex_config_args,
    claude_read_logs,
)

# CLI
from .cli import main, _build_parser, _db_path, _create_agent

__all__ = [
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
    # Types
    "AgentKind",
    "Status",
    "Actor",
    "Run",
    "Config",
    "validate_name",
    "parse_config",
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
    # Commands
    "cmd_new",
    "cmd_run",
    "cmd_interactive",
    "INTERACTIVE_PROMPT",
    "cmd_list",
    "cmd_show",
    "cmd_stop",
    "cmd_config",
    "cmd_logs",
    "cmd_discard",
    # Helpers
    "truncate",
    "format_duration",
    "worktree_path",
    "encode_dir",
    "claude_session_file_path",
    "claude_config_args",
    "codex_config_args",
    "claude_read_logs",
    # CLI
    "main",
]
