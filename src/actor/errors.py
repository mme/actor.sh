from __future__ import annotations


class ActorError(Exception):
    """Base exception for actor errors."""


class AlreadyExistsError(ActorError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"actor '{name}' already exists")


class NotFoundError(ActorError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"actor '{name}' not found")


class IsRunningError(ActorError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"actor '{name}' is currently running \u2014 stop it first")


class NotRunningError(ActorError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"actor '{name}' is not running")


class InvalidNameError(ActorError):
    def __init__(self, msg: str) -> None:
        super().__init__(f"invalid name: {msg}")


class AgentNotFoundError(ActorError):
    def __init__(self, binary: str) -> None:
        self.binary = binary
        super().__init__(f"agent binary '{binary}' not found on PATH")


class GitError(ActorError):
    def __init__(self, msg: str) -> None:
        super().__init__(f"git error: {msg}")


class ConfigError(ActorError):
    def __init__(self, msg: str) -> None:
        super().__init__(f"config error: {msg}")


class HookFailedError(ActorError):
    def __init__(self, event: str, command: str, exit_code: int) -> None:
        self.event = event
        self.command = command
        self.exit_code = exit_code
        super().__init__(
            f"{event} hook failed (exit {exit_code}): {command}"
        )
