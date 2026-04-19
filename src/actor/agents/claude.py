from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..errors import ActorError
from ..interfaces import Agent, LogEntry, LogEntryKind
from ..types import Config


class ClaudeAgent(Agent):
    def __init__(self) -> None:
        self._children: Dict[int, subprocess.Popen] = {}  # type: ignore[type-arg]
        self._lock = threading.Lock()

    # Config keys that are handled specially and not passed as CLI flags
    _INTERNAL_KEYS = {"strip-api-keys"}

    @staticmethod
    def _config_args(config: Config) -> List[str]:
        """Build common config flags. Each key becomes --key value.
        Empty values become bare flags (--key). Internal keys are skipped."""
        args: List[str] = []
        for key, value in sorted(config.items()):
            if key in ClaudeAgent._INTERNAL_KEYS:
                continue
            args.append(f"--{key}")
            if value:
                args.append(value)
        return args

    @staticmethod
    def _permission_args(config: Config) -> List[str]:
        """Build permission flags from config."""
        mode = config.get("permission-mode", "bypassPermissions")
        if mode == "bypassPermissions":
            return ["--dangerously-skip-permissions"]
        return ["--permission-mode", mode]

    def _spawn_and_track(self, args: List[str], cwd: Path, config: Config) -> int:
        strip = config.get("strip-api-keys", "true") != "false"
        if strip:
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        else:
            env = dict(os.environ)
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd),
            env=env,
        )
        pid = proc.pid
        with self._lock:
            self._children[pid] = proc
        return pid

    @staticmethod
    def _encode_dir(dir_path: Path) -> str:
        """Encode a directory path the way Claude does: replace non-alnum with -."""
        return "".join(c if c.isascii() and c.isalnum() else "-" for c in str(dir_path))

    @staticmethod
    def _session_file_path(dir_path: Path, session_id: str) -> Path:
        home = os.environ.get("HOME", "")
        if not home:
            raise ActorError("HOME environment variable is not set")
        encoded = ClaudeAgent._encode_dir(dir_path)
        return Path(home) / ".claude" / "projects" / encoded / f"{session_id}.jsonl"

    # Enable the actor MCP's channel capability in every sub-claude so spawned
    # actors inherit the same "notify me when the actor finishes" flow and can
    # orchestrate their own children identically to the top-level Claude session.
    _CHANNEL_ARGS = ["--dangerously-load-development-channels", "server:actor"]

    def start(self, dir: Path, prompt: str, config: Config) -> Tuple[int, Optional[str]]:
        session_id = str(uuid.uuid4())
        args = [
            "claude",
            *self._CHANNEL_ARGS,
            "-p",
            *self._permission_args(config),
            "--session-id",
            session_id,
            *self._config_args(config),
            "--",
            prompt,
        ]
        pid = self._spawn_and_track(args, dir, config)
        return pid, session_id

    def resume(self, dir: Path, session_id: str, prompt: str, config: Config) -> int:
        args = [
            "claude",
            *self._CHANNEL_ARGS,
            "-p",
            *self._permission_args(config),
            "--resume",
            session_id,
            *self._config_args(config),
            "--",
            prompt,
        ]
        return self._spawn_and_track(args, dir, config)

    def wait(self, pid: int) -> Tuple[int, str]:
        with self._lock:
            proc = self._children.pop(pid, None)
        if proc is None:
            raise ActorError(f"no tracked process with pid {pid}")
        stdout, _ = proc.communicate()
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        returncode = proc.returncode
        return (returncode if returncode is not None else -1, output)

    def read_logs(self, dir: Path, session_id: str) -> List[LogEntry]:
        path = self._session_file_path(dir, session_id)
        try:
            content = path.read_text()
        except FileNotFoundError:
            return []

        entries: List[LogEntry] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = v.get("type")
            if not isinstance(msg_type, str):
                continue

            timestamp = v.get("timestamp")
            if isinstance(timestamp, str):
                ts: Optional[str] = timestamp
            else:
                ts = None

            message = v.get("message")
            if message is None:
                continue

            if msg_type == "user":
                content_val = message.get("content")
                if isinstance(content_val, str):
                    entries.append(LogEntry(
                        kind=LogEntryKind.USER,
                        timestamp=ts,
                        text=content_val,
                    ))
                elif isinstance(content_val, list):
                    for item in content_val:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            c = item.get("content", "")
                            if isinstance(c, str):
                                text = c
                            else:
                                text = json.dumps(c) if c is not None else ""
                            entries.append(LogEntry(
                                kind=LogEntryKind.TOOL_RESULT,
                                timestamp=ts,
                                content=text,
                            ))

            elif msg_type == "assistant":
                content_arr = message.get("content")
                if isinstance(content_arr, list):
                    for block in content_arr:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "text":
                            text = block.get("text")
                            if isinstance(text, str):
                                entries.append(LogEntry(
                                    kind=LogEntryKind.ASSISTANT,
                                    timestamp=ts,
                                    text=text,
                                ))
                        elif block_type == "thinking":
                            text = block.get("thinking")
                            if isinstance(text, str):
                                entries.append(LogEntry(
                                    kind=LogEntryKind.THINKING,
                                    timestamp=ts,
                                    text=text,
                                ))
                        elif block_type == "tool_use":
                            name = block.get("name", "unknown")
                            inp = block.get("input")
                            inp_str = json.dumps(inp) if inp is not None else ""
                            entries.append(LogEntry(
                                kind=LogEntryKind.TOOL_USE,
                                timestamp=ts,
                                name=name,
                                input=inp_str,
                            ))

        return entries

    def interactive_argv(self, session_id: str, config: Config) -> List[str]:
        return [
            "claude",
            *self._CHANNEL_ARGS,
            *self._permission_args(config),
            "--resume", session_id,
            *self._config_args(config),
        ]

    def stop(self, pid: int) -> None:
        with self._lock:
            proc = self._children.pop(pid, None)
        if proc is not None:
            try:
                proc.kill()
            except OSError as e:
                raise ActorError(f"failed to kill pid {pid}: {e}")
            try:
                proc.wait()
            except Exception:
                pass
        else:
            # Fall back to raw signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                raise ActorError(str(e))
