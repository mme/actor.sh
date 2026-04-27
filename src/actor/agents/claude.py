from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..errors import ActorError
from ..interfaces import Agent, LogEntry, LogEntryKind
from ..types import ActorConfig
from ._jsonl import (
    iter_lines_with_offsets as _iter_lines_with_offsets,
    split_complete_lines_with_offsets,
)


class ClaudeAgent(Agent):
    AGENT_DEFAULTS: Dict[str, str] = {
        "permission-mode": "auto",
    }
    ACTOR_DEFAULTS: Dict[str, str] = {
        "use-subscription": "true",
    }

    def __init__(self) -> None:
        self._children: Dict[int, subprocess.Popen] = {}  # type: ignore[type-arg]
        self._lock = threading.Lock()

    def emit_agent_args(self, defaults: Dict[str, str]) -> List[str]:
        """Map the resolved `agent_args` dict to claude CLI flags.

        Straight mapping: `key "value"` → `--key value`; empty string becomes
        a bare `--key`. `None` values are skipped defensively — ActorConfig
        is supposed to contain only concrete strings (the cmd_new resolver
        strips kdl-layer `None` cancel markers), but we drop them here too
        so a misrouted caller can't feed `None` into subprocess.Popen."""
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

    def _spawn_and_track(self, args: List[str], cwd: Path, config: ActorConfig) -> int:
        env = self.apply_actor_keys(config.actor_keys, os.environ)
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

    def start(self, dir: Path, prompt: str, config: ActorConfig) -> Tuple[int, Optional[str]]:
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
        pid = self._spawn_and_track(args, dir, config)
        return pid, session_id

    def resume(self, dir: Path, session_id: str, prompt: str, config: ActorConfig) -> int:
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

    def session_file_size(
        self, dir: Path, session_id: str,
    ) -> Optional[int]:
        """Byte size of the session JSONL if it exists, else ``None``.
        Used by run-boundary offset capture to bracket each run's
        contribution to the file."""
        try:
            return self._session_file_path(dir, session_id).stat().st_size
        except (FileNotFoundError, OSError):
            return None

    def read_logs(self, dir: Path, session_id: str) -> List[LogEntry]:
        path = self._session_file_path(dir, session_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return []
        return self._parse_entries(_iter_lines_with_offsets(data, 0))

    def read_logs_since(
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

        Each emitted ``LogEntry`` carries its absolute source offset
        so downstream readers can bucket it back into the ``Run`` row
        whose ``[log_start_offset, log_end_offset)`` bracket contains
        it.
        """
        path = self._session_file_path(dir, session_id)
        try:
            size = path.stat().st_size
        except (FileNotFoundError, OSError):
            return [], cursor

        if isinstance(cursor, int) and 0 <= cursor <= size:
            offset = cursor
        else:
            offset = 0

        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return [], cursor

        lines, cursor_advance = split_complete_lines_with_offsets(data, offset)
        if cursor_advance is None:
            # No newline in the chunk at all — defer entirely.
            return [], cursor
        new_cursor = offset + cursor_advance
        return self._parse_entries(lines), new_cursor

    @staticmethod
    def _parse_entries(
        lines: "Any",
    ) -> List[LogEntry]:
        """Parse an iterable of ``(absolute_offset, line_text)`` pairs
        into LogEntry instances. Each entry carries the offset of the
        JSONL line it was decoded from.

        Accepts an iterable rather than a list so both the
        streaming-tail path (``split_complete_lines_with_offsets``)
        and the full-read path (``_iter_lines_with_offsets``) can
        plug in directly."""
        entries: List[LogEntry] = []
        for offset, line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                v = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            entries.extend(ClaudeAgent._parse_log_dict(v, offset))
        return entries

    @staticmethod
    def _parse_log_dict(v: dict, offset: Optional[int] = None) -> List[LogEntry]:
        """Parse one decoded JSONL record into zero or more LogEntry.

        All entries produced from the same source line share the same
        ``source_offset`` — they collectively represent that line's
        contribution to the visible log and therefore belong to the
        same run bucket."""
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
                    source_offset=offset,
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
                            source_offset=offset,
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
                            out.append(LogEntry(
                                kind=LogEntryKind.ASSISTANT,
                                timestamp=ts,
                                text=text,
                                source_offset=offset,
                            ))
                    elif block_type == "thinking":
                        text = block.get("thinking")
                        if isinstance(text, str):
                            out.append(LogEntry(
                                kind=LogEntryKind.THINKING,
                                timestamp=ts,
                                text=text,
                                source_offset=offset,
                            ))
                    elif block_type == "tool_use":
                        name = block.get("name", "unknown")
                        inp = block.get("input")
                        inp_str = json.dumps(inp) if inp is not None else ""
                        out.append(LogEntry(
                            kind=LogEntryKind.TOOL_USE,
                            timestamp=ts,
                            name=name,
                            input=inp_str,
                            source_offset=offset,
                        ))

        return out

    def interactive_argv(self, session_id: str, config: ActorConfig) -> List[str]:
        return [
            "claude",
            *self._CHANNEL_ARGS,
            "--resume", session_id,
            *self.emit_agent_args(config.agent_args),
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
