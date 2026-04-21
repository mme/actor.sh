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
from typing import Dict, List, Mapping, Optional, Tuple

from ..errors import ActorError
from ..interfaces import Agent, LogEntry, LogEntryKind
from ..types import Config


class _CodexChild:
    def __init__(self, proc: subprocess.Popen, relay: Optional[threading.Thread]) -> None:  # type: ignore[type-arg]
        self.proc = proc
        self.relay = relay
        self.output_lines: List[str] = []


class CodexAgent(Agent):
    AGENT_DEFAULTS: Dict[str, Optional[str]] = {
        "sandbox": "danger-full-access",
        "a": "never",
    }
    ACTOR_DEFAULTS: Dict[str, Optional[str]] = {
        "use-subscription": "true",
    }

    def __init__(self) -> None:
        self._children: Dict[int, _CodexChild] = {}
        self._lock = threading.Lock()

    def emit_agent_args(self, defaults: Config) -> List[str]:
        """Map the resolved `defaults { }` dict to codex CLI flags.

        Users write Codex's native flag names verbatim (e.g. `m`, `a`,
        `sandbox`). One-character keys emit a short flag (`-k value`);
        longer keys emit a long flag (`--key value`). Empty string becomes
        a bare flag. `defaults` is `Config` (Dict[str, str]) — the kdl-layer
        `None` cancel markers are stripped in cmd_new before this runs."""
        args: List[str] = []
        for key, value in sorted(defaults.items()):
            prefix = "-" if len(key) == 1 else "--"
            args.append(f"{prefix}{key}")
            if value != "":
                args.append(value)
        return args

    def apply_actor_keys(
        self, flat: Config, env: Mapping[str, str]
    ) -> Dict[str, str]:
        """Strip OPENAI_API_KEY from the env when use-subscription is on
        (default true) so Codex falls back to the logged-in subscription."""
        out = dict(env)
        if flat.get("use-subscription", "true") != "false":
            out.pop("OPENAI_API_KEY", None)
        return out

    def _spawn_and_capture(
        self, args: List[str], cwd: Optional[Path], config: Config
    ) -> Tuple[int, Optional[str]]:
        actor_keys, _ = self._split_config(config)
        env = self.apply_actor_keys(actor_keys, os.environ)
        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=None,  # inherit
            env=env,
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

        child = _CodexChild(proc, None)

        # Spawn a thread to relay remaining stdout
        def _relay() -> None:
            try:
                while True:
                    line = reader.readline()
                    if not line:
                        break
                    try:
                        v = json.loads(line.decode("utf-8", errors="replace").strip())
                        event_type = v.get("type")
                        # Codex emits item.completed with agent_message items
                        if event_type == "item.completed":
                            item = v.get("item", {})
                            if item.get("type") == "agent_message":
                                text = item.get("text", "")
                                if text:
                                    child.output_lines.append(text)
                                    sys.stdout.write(text + "\n")
                                    sys.stdout.flush()
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
            except Exception:
                pass

        relay_thread = threading.Thread(target=_relay, daemon=True)
        relay_thread.start()
        child.relay = relay_thread

        with self._lock:
            self._children[pid] = child

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
        _, agent_args = self._split_config(config)
        args = [
            "codex",
            "exec",
            "--json",
            "-C",
            str(dir),
            *self.emit_agent_args(agent_args),
            prompt,
        ]
        return self._spawn_and_capture(args, cwd=None, config=config)

    def resume(self, dir: Path, session_id: str, prompt: str, config: Config) -> int:
        _, agent_args = self._split_config(config)
        args = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--json",
            *self.emit_agent_args(agent_args),
            prompt,
        ]
        pid, _ = self._spawn_and_capture(args, cwd=dir, config=config)
        return pid

    def wait(self, pid: int) -> Tuple[int, str]:
        with self._lock:
            entry = self._children.pop(pid, None)
        if entry is None:
            raise ActorError(f"no tracked process with pid {pid}")
        returncode = entry.proc.wait()
        if entry.relay is not None:
            entry.relay.join(timeout=5)
        output = "\n".join(entry.output_lines)
        return (returncode if returncode is not None else -1, output)

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

            top_type = v.get("type")
            payload = v.get("payload", {})

            if top_type == "event_msg":
                msg_type = payload.get("type")
                if msg_type == "agent_message":
                    text = payload.get("message", "")
                    if isinstance(text, str) and text:
                        entries.append(LogEntry(
                            kind=LogEntryKind.ASSISTANT,
                            text=text,
                        ))
                elif msg_type == "user_message":
                    text = payload.get("message", "")
                    if isinstance(text, str) and text:
                        entries.append(LogEntry(
                            kind=LogEntryKind.USER,
                            text=text,
                        ))
                elif msg_type == "exec_command":
                    cmd = payload.get("command", {})
                    cmd_str = cmd.get("command", "") if isinstance(cmd, dict) else ""
                    if isinstance(cmd_str, str) and cmd_str:
                        entries.append(LogEntry(
                            kind=LogEntryKind.TOOL_USE,
                            name="shell",
                            input=cmd_str,
                        ))
                elif msg_type == "exec_command_output":
                    output = payload.get("output", "")
                    if isinstance(output, str) and output:
                        entries.append(LogEntry(
                            kind=LogEntryKind.TOOL_RESULT,
                            content=output,
                        ))
            elif top_type == "response_item":
                item_type = payload.get("type")
                if item_type == "reasoning":
                    summaries = payload.get("summary", [])
                    for s in summaries:
                        text = s.get("text", "") if isinstance(s, dict) else ""
                        if isinstance(text, str) and text:
                            entries.append(LogEntry(
                                kind=LogEntryKind.THINKING,
                                text=text,
                            ))

        return entries

    def interactive_argv(self, session_id: str, config: Config) -> List[str]:
        # Propagate agent flags so interactive sessions honor the same
        # defaults as non-interactive runs (parity with Claude).
        _, agent_args = self._split_config(config)
        return [
            "codex", "resume", session_id,
            *self.emit_agent_args(agent_args),
        ]

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
