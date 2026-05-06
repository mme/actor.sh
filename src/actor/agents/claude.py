from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..errors import ActorError
from ..interfaces import Agent, LogEntry, LogEntryKind
from ..types import ActorConfig
from ._jsonl import split_complete_lines


class ClaudeAgent(Agent):
    AGENT_DEFAULTS: Dict[str, str] = {
        "permission-mode": "auto",
    }
    ACTOR_DEFAULTS: Dict[str, str] = {
        "use-subscription": "true",
    }
    # The agent_args key under which a role's `prompt` field gets injected.
    # claude has `--append-system-prompt` to layer extra instructions on top
    # of Claude Code's defaults — exactly the right shape for "role
    # personality + Claude Code's standard tools".
    SYSTEM_PROMPT_KEY: Optional[str] = "append-system-prompt"

    def __init__(self) -> None:
        self._children: Dict[int, asyncio.subprocess.Process] = {}

    def emit_agent_args(self, defaults: Dict[str, str]) -> List[str]:
        """Map the resolved `agent_args` dict to claude CLI flags.

        Straight mapping: `key "value"` → `--key value`; empty string becomes
        a bare `--key`. `None` values are skipped defensively — ActorConfig
        is supposed to contain only concrete strings (the cmd_new resolver
        strips kdl-layer `None` cancel markers), but we drop them here too
        so a misrouted caller can't feed `None` into create_subprocess_exec."""
        args: List[str] = []
        for key, value in sorted(defaults.items()):
            if value is None:
                continue
            args.append(f"--{key}")
            if value != "":
                args.append(value)
        return args

    def apply_actor_keys(
        self, actor_keys: Dict[str, str], env: Mapping[str, str]
    ) -> Dict[str, str]:
        """Strip ANTHROPIC_API_KEY from the env when use-subscription is on
        (default true) so Claude falls back to the logged-in subscription."""
        out = dict(env)
        if actor_keys.get("use-subscription", "true") != "false":
            out.pop("ANTHROPIC_API_KEY", None)
        return out

    async def _spawn_and_track(
        self, args: List[str], cwd: Path, config: ActorConfig,
    ) -> int:
        env = self.apply_actor_keys(config.actor_keys, os.environ)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env=env,
        )
        pid = proc.pid
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

    async def start(
        self, dir: Path, prompt: str, config: ActorConfig,
    ) -> Tuple[int, Optional[str]]:
        session_id = str(uuid.uuid4())
        args = [
            "claude",
            *self._CHANNEL_ARGS,
            "-p",
            "--session-id",
            session_id,
            *self.emit_agent_args(config.agent_args),
            "--",
            prompt,
        ]
        pid = await self._spawn_and_track(args, dir, config)
        return pid, session_id

    async def resume(
        self, dir: Path, session_id: str, prompt: str, config: ActorConfig,
    ) -> int:
        args = [
            "claude",
            *self._CHANNEL_ARGS,
            "-p",
            "--resume",
            session_id,
            *self.emit_agent_args(config.agent_args),
            "--",
            prompt,
        ]
        return await self._spawn_and_track(args, dir, config)

    async def wait(self, pid: int) -> Tuple[int, str]:
        proc = self._children.pop(pid, None)
        if proc is None:
            raise ActorError(f"no tracked process with pid {pid}")
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        returncode = proc.returncode
        return (returncode if returncode is not None else -1, output)

    async def read_logs(self, dir: Path, session_id: str) -> List[LogEntry]:
        path = self._session_file_path(dir, session_id)

        def _read() -> Optional[str]:
            try:
                return path.read_text()
            except FileNotFoundError:
                return None

        content = await asyncio.to_thread(_read)
        if content is None:
            return []
        return self._parse_entries(content)

    async def read_logs_since(
        self, dir: Path, session_id: str, cursor: Any = None,
    ) -> Tuple[List[LogEntry], Any]:
        """Read the tail of the session JSONL starting from a byte
        offset cursor. Cursor semantics:

        - ``cursor=None`` or an out-of-range int → full read from byte 0.
        - ``cursor=N`` → seek to N and read to EOF.
        - Returned cursor points to the end of the last **complete**
          (newline-terminated) line we parsed. Any partial tail is
          left for the next call to pick up once more bytes arrive.
        - File shrunk below cursor (rotation, truncation) → treat
          cursor as stale and full-read from 0.
        """
        path = self._session_file_path(dir, session_id)

        def _read_tail() -> Tuple[Optional[bytes], int]:
            try:
                size = path.stat().st_size
            except (FileNotFoundError, OSError):
                return None, 0
            if isinstance(cursor, int) and 0 <= cursor <= size:
                offset = cursor
            else:
                offset = 0
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    data = f.read()
            except OSError:
                return None, offset
            return data, offset

        data, offset = await asyncio.to_thread(_read_tail)
        if data is None:
            return [], cursor

        text, cursor_advance = split_complete_lines(data)
        if cursor_advance is None:
            # No newline in the chunk at all — defer entirely.
            return [], cursor
        new_cursor = offset + cursor_advance
        return self._parse_entries(text), new_cursor

    @staticmethod
    def _parse_entries(content: str) -> List[LogEntry]:
        """Parse Claude JSONL text into LogEntry instances. Shared
        between the full-read path (``read_logs``) and the streaming
        tail path (``read_logs_since``); also re-exported via
        ``claude_read_logs`` for direct-path-based tests."""
        entries: List[LogEntry] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.extend(ClaudeAgent._parse_log_dict(v))
        return entries

    @staticmethod
    def _parse_log_dict(v: dict) -> List[LogEntry]:
        """Parse one decoded JSONL record into zero or more LogEntry."""
        out: List[LogEntry] = []

        msg_type = v.get("type")
        if not isinstance(msg_type, str):
            return out

        timestamp = v.get("timestamp")
        ts: Optional[str] = timestamp if isinstance(timestamp, str) else None

        message = v.get("message")
        if message is None:
            return out

        if msg_type == "user":
            content_val = message.get("content")
            if isinstance(content_val, str):
                out.append(LogEntry(
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
                        out.append(LogEntry(
                            kind=LogEntryKind.TOOL_RESULT,
                            timestamp=ts,
                            content=text,
                        ))

        elif msg_type == "assistant":
            content_arr = message.get("content")
            # Token usage lives at the message level (per LLM call),
            # not the block level. Attach it to the first content
            # block that produces a LogEntry so a downstream sum
            # doesn't double-count multi-block messages (e.g. a
            # message that emits both `thinking` and `text`).
            raw_usage = message.get("usage") if isinstance(message, dict) else None
            usage = raw_usage if isinstance(raw_usage, dict) else None
            usage_attached = False
            if isinstance(content_arr, list):
                for block in content_arr:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            entry = LogEntry(
                                kind=LogEntryKind.ASSISTANT,
                                timestamp=ts,
                                text=text,
                            )
                            if usage is not None and not usage_attached:
                                entry.usage = usage
                                usage_attached = True
                            out.append(entry)
                    elif block_type == "thinking":
                        text = block.get("thinking")
                        if isinstance(text, str):
                            entry = LogEntry(
                                kind=LogEntryKind.THINKING,
                                timestamp=ts,
                                text=text,
                            )
                            if usage is not None and not usage_attached:
                                entry.usage = usage
                                usage_attached = True
                            out.append(entry)
                    elif block_type == "tool_use":
                        name = block.get("name", "unknown")
                        inp = block.get("input")
                        inp_str = json.dumps(inp) if inp is not None else ""
                        entry = LogEntry(
                            kind=LogEntryKind.TOOL_USE,
                            timestamp=ts,
                            name=name,
                            input=inp_str,
                        )
                        if usage is not None and not usage_attached:
                            entry.usage = usage
                            usage_attached = True
                        out.append(entry)

        return out

    def interactive_argv(self, session_id: str, config: ActorConfig) -> List[str]:
        return [
            "claude",
            *self._CHANNEL_ARGS,
            "--resume", session_id,
            *self.emit_agent_args(config.agent_args),
        ]

    async def stop(self, pid: int) -> None:
        proc = self._children.pop(pid, None)
        if proc is not None:
            try:
                proc.kill()
            except (OSError, ProcessLookupError) as e:
                raise ActorError(f"failed to kill pid {pid}: {e}")
            try:
                await proc.wait()
            except Exception:
                pass
        else:
            # Fall back to raw signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                raise ActorError(str(e))
