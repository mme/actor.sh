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


class InteractiveSessionEnded(ActorError):
    """Raised when an `InteractiveSession`'s bidi stream breaks
    mid-session (daemon crashed / restarted). The PTY's state lives
    in the daemon; there's nothing to reconnect to. CLI prints a
    clear message and exits non-zero."""


class DaemonUnreachableError(ActorError):
    """Raised by `RemoteActorService` when actord isn't accepting
    connections. CLI / MCP bridge translate this into a one-line "start
    it with: actor daemon start" message before exiting non-zero."""
    def __init__(self, socket_path: str, cause: BaseException | None = None) -> None:
        self.socket_path = socket_path
        self.cause = cause
        suffix = f" ({cause})" if cause is not None else ""
        super().__init__(
            f"actord not reachable at {socket_path}{suffix}; "
            f"start it with: actor daemon start"
        )


class HookFailedError(ActorError):
    def __init__(
        self,
        event: str,
        command: str,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.event = event
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        msg = f"{event} hook failed (exit {exit_code}): {command}"
        tail = _format_output_tail(stdout, stderr)
        if tail:
            msg = f"{msg}\n{tail}"
        super().__init__(msg)


# Cap how much captured hook output we embed in the error message — enough
# context for debugging, not so much that a chatty hook buries the actual
# failure when this surfaces in the TUI or MCP response.
_HOOK_OUTPUT_TAIL_LINES = 10
_HOOK_OUTPUT_TAIL_CHARS = 800


def _format_output_tail(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    for label, text in (("stderr", stderr), ("stdout", stdout)):
        trimmed = _tail(text)
        if trimmed:
            parts.append(f"  {label} tail:\n{_indent(trimmed)}")
    return "\n".join(parts)


def _tail(text: str) -> str:
    text = text.rstrip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > _HOOK_OUTPUT_TAIL_LINES:
        lines = lines[-_HOOK_OUTPUT_TAIL_LINES:]
    tail = "\n".join(lines)
    if len(tail) > _HOOK_OUTPUT_TAIL_CHARS:
        tail = tail[-_HOOK_OUTPUT_TAIL_CHARS:]
    return tail


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())
