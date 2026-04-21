from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from ..errors import ActorError
from ..interfaces import Agent, LogEntry, LogEntryKind
from ..types import Config


class ClaudeAgent(Agent):
    AGENT_DEFAULTS: Dict[str, Optional[str]] = {
        "permission-mode": "auto",
    }
    ACTOR_DEFAULTS: Dict[str, Optional[str]] = {
        "use-subscription": "true",
    }

    def __init__(self) -> None:
        self._children: Dict[int, subprocess.Popen] = {}  # type: ignore[type-arg]
        self._lock = threading.Lock()

    def emit_agent_args(self, defaults: Config) -> List[str]:
        """Map the resolved `defaults { }` dict to claude CLI flags.

        Straight mapping: `key "value"` → `--key value`; empty string becomes
        a bare `--key`. `defaults` is `Config` (Dict[str, str]) — the kdl-layer
        `None` cancel markers are stripped in cmd_new before this runs."""
        args: List[str] = []
        for key, value in sorted(defaults.items()):
            args.append(f"--{key}")
            if value != "":
                args.append(value)
        return args

    def apply_actor_keys(
        self, flat: Config, env: Mapping[str, str]
    ) -> Dict[str, str]:
        """Strip ANTHROPIC_API_KEY from the env when use-subscription is on
        (default true) so Claude falls back to the logged-in subscription."""
        out = dict(env)
        if flat.get("use-subscription", "true") != "false":
            out.pop("ANTHROPIC_API_KEY", None)
        return out

    def _spawn_and_track(self, args: List[str], cwd: Path, config: Config) -> int:
        actor_keys, _ = self._split_config(config)
        env = self.apply_actor_keys(actor_keys, os.environ)
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
        _, agent_args = self._split_config(config)
        session_id = str(uuid.uuid4())
        args = [
            "claude",
            *self._CHANNEL_ARGS,
            "-p",
            "--session-id",
            session_id,
            *self.emit_agent_args(agent_args),
            "--",
            prompt,
        ]
        pid = self._spawn_and_track(args, dir, config)
        return pid, session_id

    def resume(self, dir: Path, session_id: str, prompt: str, config: Config) -> int:
        _, agent_args = self._split_config(config)
        args = [
            "claude",
            *self._CHANNEL_ARGS,
            "-p",
            "--resume",
            session_id,
            *self.emit_agent_args(agent_args),
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
        _, agent_args = self._split_config(config)
        return [
            "claude",
            *self._CHANNEL_ARGS,
            "--resume", session_id,
            *self.emit_agent_args(agent_args),
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
