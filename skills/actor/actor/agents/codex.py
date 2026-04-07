from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..errors import ActorError
from ..interfaces import Agent, LogEntry, LogEntryKind
from ..types import Config


class _CodexChild:
    def __init__(self, proc: subprocess.Popen, relay: Optional[threading.Thread]) -> None:  # type: ignore[type-arg]
        self.proc = proc
        self.relay = relay


class CodexAgent(Agent):
    def __init__(self) -> None:
        self._children: Dict[int, _CodexChild] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _config_args(config: Config) -> List[str]:
        """Build config flags for Codex.
        model key becomes -m <value>, all others become -c key=value.
        """
        args: List[str] = []
        for key, value in sorted(config.items()):
            if key == "model":
                args.append("-m")
                args.append(value)
            else:
                args.append("-c")
                args.append(f"{key}={value}")
        return args

    def _spawn_and_capture(
        self, args: List[str], cwd: Optional[Path]
    ) -> Tuple[int, Optional[str]]:
        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,  # inherit
        )
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        proc = subprocess.Popen(args, **kwargs)
        pid = proc.pid

        stdout = proc.stdout
        if stdout is None:
            raise ActorError("failed to capture codex stdout")

        # Read the first line to get thread_id
        reader = io.BufferedReader(stdout)  # type: ignore[arg-type]
        first_line = b""
        try:
            first_line = reader.readline()
        except Exception:
            pass

        thread_id: Optional[str] = None
        if first_line:
            try:
                v = json.loads(first_line.decode("utf-8", errors="replace").strip())
                tid = v.get("thread_id")
                if isinstance(tid, str):
                    thread_id = tid
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        # Spawn a thread to relay remaining stdout
        def _relay() -> None:
            try:
                while True:
                    line = reader.readline()
                    if not line:
                        break
                    try:
                        v = json.loads(line.decode("utf-8", errors="replace").strip())
                        if v.get("type") == "message.output_text.delta":
                            delta = v.get("delta")
                            if isinstance(delta, str):
                                sys.stdout.write(delta)
                                sys.stdout.flush()
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
            except Exception:
                pass

        relay_thread = threading.Thread(target=_relay, daemon=True)
        relay_thread.start()

        with self._lock:
            self._children[pid] = _CodexChild(proc, relay_thread)

        return pid, thread_id

    @staticmethod
    def _find_rollout_path(session_id: str) -> Optional[Path]:
        home = os.environ.get("HOME", "")
        if not home:
            return None
        db_path = Path(home) / ".codex" / "state_5.sqlite"
        if not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
            )
            cur = conn.execute(
                "SELECT rollout_path FROM threads WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            conn.close()
            if row is None:
                return None
            return Path(row[0])
        except Exception as e:
            raise ActorError(f"codex DB query failed: {e}")

    def start(self, dir: Path, prompt: str, config: Config) -> Tuple[int, Optional[str]]:
        args = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-C",
            str(dir),
            *self._config_args(config),
            prompt,
        ]
        return self._spawn_and_capture(args, cwd=None)

    def resume(self, dir: Path, session_id: str, prompt: str, config: Config) -> int:
        args = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            *self._config_args(config),
            prompt,
        ]
        pid, _ = self._spawn_and_capture(args, cwd=dir)
        return pid

    def wait(self, pid: int) -> int:
        with self._lock:
            entry = self._children.pop(pid, None)
        if entry is None:
            raise ActorError(f"no tracked process with pid {pid}")
        returncode = entry.proc.wait()
        if entry.relay is not None:
            entry.relay.join(timeout=5)
        return returncode if returncode is not None else -1

    def read_logs(self, dir: Path, session_id: str) -> List[LogEntry]:
        rollout_path = self._find_rollout_path(session_id)
        if rollout_path is None:
            return []

        try:
            content = rollout_path.read_text()
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

            event_type = v.get("type")
            if not isinstance(event_type, str):
                continue

            if event_type == "message.output_text.done":
                text = v.get("text", "")
                if isinstance(text, str) and text:
                    entries.append(LogEntry(
                        kind=LogEntryKind.ASSISTANT,
                        text=text,
                    ))
            elif event_type in ("input_text", "message.input_text"):
                text = v.get("text", "")
                if isinstance(text, str) and text:
                    entries.append(LogEntry(
                        kind=LogEntryKind.USER,
                        text=text,
                    ))
            elif event_type in ("function_call", "message.function_call"):
                name = v.get("name", "unknown")
                args_val = v.get("arguments")
                args_str = json.dumps(args_val) if args_val is not None else ""
                entries.append(LogEntry(
                    kind=LogEntryKind.TOOL_USE,
                    name=name if isinstance(name, str) else "unknown",
                    input=args_str,
                ))
            elif event_type in ("function_call_output", "message.function_call_output"):
                output = v.get("output", "")
                entries.append(LogEntry(
                    kind=LogEntryKind.TOOL_RESULT,
                    content=output if isinstance(output, str) else "",
                ))
            elif event_type == "reasoning.summary_text.done":
                text = v.get("text", "")
                if isinstance(text, str) and text:
                    entries.append(LogEntry(
                        kind=LogEntryKind.THINKING,
                        text=text,
                    ))

        return entries

    def stop(self, pid: int) -> None:
        with self._lock:
            entry = self._children.pop(pid, None)
        if entry is not None:
            try:
                entry.proc.kill()
            except OSError as e:
                raise ActorError(f"failed to kill pid {pid}: {e}")
            try:
                entry.proc.wait()
            except Exception:
                pass
            if entry.relay is not None:
                entry.relay.join(timeout=5)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                raise ActorError(str(e))
