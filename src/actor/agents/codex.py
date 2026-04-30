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
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..errors import ActorError
from ..interfaces import Agent, LogEntry, LogEntryKind
from ..types import ActorConfig
from ._jsonl import split_complete_lines


def _coerce_int(v: Any) -> int:
    """Codex's token counts arrive as ints in practice, but the
    schema says nothing — defend against null / float / missing so
    a malformed record doesn't propagate `None` into the running
    sum (which `_aggregate_token_usage` skips, silently zero-ing
    the actor's totals)."""
    return v if isinstance(v, int) else 0


class _CodexChild:
    def __init__(self, proc: subprocess.Popen, relay: Optional[threading.Thread]) -> None:  # type: ignore[type-arg]
        self.proc = proc
        self.relay = relay
        self.output_lines: List[str] = []


class CodexAgent(Agent):
    AGENT_DEFAULTS: Dict[str, str] = {
        "sandbox": "danger-full-access",
        "a": "never",
    }
    ACTOR_DEFAULTS: Dict[str, str] = {
        "use-subscription": "true",
    }

    def __init__(self) -> None:
        self._children: Dict[int, _CodexChild] = {}
        self._lock = threading.Lock()

    def emit_agent_args(self, defaults: Dict[str, str]) -> List[str]:
        """Map the resolved `agent_args` dict to codex CLI flags.

        Users write Codex's native flag names verbatim (e.g. `m`, `a`,
        `sandbox`). One-character keys emit a short flag (`-k value`);
        longer keys emit a long flag (`--key value`). Empty string becomes
        a bare flag. `None` values are skipped defensively — ActorConfig
        is supposed to contain only concrete strings (the cmd_new resolver
        strips kdl-layer `None` cancel markers), but we drop them here too
        so a misrouted caller can't feed `None` into subprocess.Popen."""
        args: List[str] = []
        for key, value in sorted(defaults.items()):
            if value is None:
                continue
            prefix = "-" if len(key) == 1 else "--"
            args.append(f"{prefix}{key}")
            if value != "":
                args.append(value)
        return args

    def apply_actor_keys(
        self, actor_keys: Dict[str, str], env: Mapping[str, str]
    ) -> Dict[str, str]:
        """Strip OPENAI_API_KEY from the env when use-subscription is on
        (default true) so Codex falls back to the logged-in subscription."""
        out = dict(env)
        if actor_keys.get("use-subscription", "true") != "false":
            out.pop("OPENAI_API_KEY", None)
        return out

    def _spawn_and_capture(
        self, args: List[str], cwd: Optional[Path], config: ActorConfig
    ) -> Tuple[int, Optional[str]]:
        env = self.apply_actor_keys(config.actor_keys, os.environ)
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

    def start(self, dir: Path, prompt: str, config: ActorConfig) -> Tuple[int, Optional[str]]:
        # Agent args go BEFORE `exec` so they're parsed as parent-codex
        # flags. The parent CLI accepts the union of every flag we emit
        # (-a, -m, -s, -c, -p, -i); the `exec` subcommand omits `-a`,
        # so flags placed after `exec` would fail with "unexpected
        # argument '-a'" the moment a default like `a=never` is in
        # play. Putting them all at the parent level sidesteps the
        # split entirely and matches how `codex` is documented for
        # interactive use.
        args = [
            "codex",
            *self.emit_agent_args(config.agent_args),
            "exec",
            "--json",
            "-C",
            str(dir),
            prompt,
        ]
        return self._spawn_and_capture(args, cwd=None, config=config)

    def resume(self, dir: Path, session_id: str, prompt: str, config: ActorConfig) -> int:
        # Same parent-flags-before-`exec` ordering as `start` — see the
        # comment there for why.
        args = [
            "codex",
            *self.emit_agent_args(config.agent_args),
            "exec",
            "resume",
            session_id,
            "--json",
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
        return self._parse_entries(content)

    def read_logs_since(
        self, dir: Path, session_id: str, cursor: Any = None,
    ) -> Tuple[List[LogEntry], Any]:
        """Byte-offset cursor tail read against the Codex rollout file.
        Same semantics as ClaudeAgent.read_logs_since — see that
        docstring for cursor behavior."""
        rollout_path = self._find_rollout_path(session_id)
        if rollout_path is None:
            return [], cursor
        try:
            size = rollout_path.stat().st_size
        except (FileNotFoundError, OSError):
            return [], cursor

        if isinstance(cursor, int) and 0 <= cursor <= size:
            offset = cursor
        else:
            offset = 0

        try:
            with open(rollout_path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return [], cursor

        text, cursor_advance = split_complete_lines(data)
        if cursor_advance is None:
            return [], cursor
        new_cursor = offset + cursor_advance
        return self._parse_entries(text), new_cursor

    @staticmethod
    def _parse_entries(content: str) -> List[LogEntry]:
        """Parse Codex JSONL text into LogEntry instances. Shared
        between the full-read path (``read_logs``) and the streaming
        tail path (``read_logs_since``).

        Token usage attaches retroactively: when a ``token_count``
        record is parsed, the per-turn delta (``last_token_usage``)
        is stored on the most recent ASSISTANT entry already in the
        running list. That mirrors Claude's "usage on first content
        block of a message" model — `_aggregate_token_usage` can sum
        across actors uniformly without an agent-aware code path."""
        entries: List[LogEntry] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            new_entries, usage = CodexAgent._parse_log_dict(v)
            entries.extend(new_entries)
            if usage is not None:
                CodexAgent._attach_usage(entries, usage)
        return entries

    @staticmethod
    def _attach_usage(entries: List[LogEntry], usage: Dict[str, int]) -> None:
        """Attach ``usage`` to the most recent ASSISTANT entry that
        doesn't already carry it. No-op when no eligible target —
        token_count records can arrive before the first agent reply
        (Codex emits a `token_count` with `info=null` immediately
        after `task_started`); summing skips the orphan, which is
        correct (no real tokens consumed yet)."""
        for entry in reversed(entries):
            if entry.kind == LogEntryKind.ASSISTANT and entry.usage is None:
                entry.usage = usage
                return

    @staticmethod
    def _parse_log_dict(
        v: dict,
    ) -> Tuple[List[LogEntry], Optional[Dict[str, int]]]:
        """Parse one decoded Codex JSONL record into zero or more
        LogEntry instances, plus an optional usage dict to attach to
        the prior ASSISTANT entry (returned separately because the
        usage event is its own line and has no inline message text).

        Codex emits two parallel streams in one JSONL: ``event_msg``
        (UI events: agent_message, user_message, token_count, etc.)
        and ``response_item`` (the raw model response, including
        reasoning + tool calls + tool outputs). User-typed prompts
        and final agent replies appear in BOTH streams; we read them
        from ``event_msg`` (simpler shape) and skip the duplicate
        ``response_item.message`` records. Tool calls only appear in
        ``response_item`` so we read those from there. The
        ``event_msg.exec_command_end`` / ``patch_apply_end`` events
        are status notifications that piggy-back on the canonical
        ``response_item.*_call_output`` records — skipping them
        avoids duplicating tool-result entries."""
        out: List[LogEntry] = []
        top_type = v.get("type")
        payload = v.get("payload", {})
        if not isinstance(payload, dict):
            return out, None

        # Top-level timestamp on every record. Claude carries it on
        # the inner `message`; Codex on the outer record. Either way
        # we pass it through to every LogEntry the record produces.
        ts_raw = v.get("timestamp")
        ts: Optional[str] = ts_raw if isinstance(ts_raw, str) else None

        if top_type == "event_msg":
            sub = payload.get("type")
            if sub == "agent_message":
                text = payload.get("message", "")
                if isinstance(text, str) and text:
                    out.append(LogEntry(
                        kind=LogEntryKind.ASSISTANT,
                        timestamp=ts,
                        text=text,
                    ))
            elif sub == "user_message":
                text = payload.get("message", "")
                if isinstance(text, str) and text:
                    out.append(LogEntry(
                        kind=LogEntryKind.USER,
                        timestamp=ts,
                        text=text,
                    ))
            elif sub == "token_count":
                # Per-turn delta only — `total_token_usage` is
                # cumulative and would over-count if summed. The
                # `info=null` startup ping returns no usage.
                info = payload.get("info")
                if isinstance(info, dict):
                    last = info.get("last_token_usage")
                    if isinstance(last, dict):
                        usage = {
                            "input_tokens": _coerce_int(last.get("input_tokens")),
                            "output_tokens": _coerce_int(last.get("output_tokens")),
                        }
                        return out, usage
        elif top_type == "response_item":
            sub = payload.get("type")
            if sub == "reasoning":
                # Modern Codex emits empty `summary[]` plus an
                # `encrypted_content` blob we can't decode. Fall back
                # silently when summary is empty rather than emitting
                # phantom THINKING rows.
                for s in payload.get("summary", []) or []:
                    text = s.get("text", "") if isinstance(s, dict) else ""
                    if isinstance(text, str) and text:
                        out.append(LogEntry(
                            kind=LogEntryKind.THINKING,
                            timestamp=ts,
                            text=text,
                        ))
            elif sub == "function_call":
                # Generic tool call: `arguments` is already a JSON
                # string (matches Claude's tool_use shape, which also
                # stores input as a JSON string). Most common in
                # practice is exec_command with shell args.
                name = payload.get("name") or "unknown"
                args = payload.get("arguments")
                input_str = args if isinstance(args, str) else (
                    json.dumps(args) if args is not None else ""
                )
                out.append(LogEntry(
                    kind=LogEntryKind.TOOL_USE,
                    timestamp=ts,
                    name=name,
                    input=input_str,
                ))
            elif sub == "custom_tool_call":
                # Built-in non-function tools (notably apply_patch).
                # `input` is raw text (a patch body, etc.) — pass
                # through verbatim.
                name = payload.get("name") or "unknown"
                inp = payload.get("input")
                input_str = inp if isinstance(inp, str) else (
                    json.dumps(inp) if inp is not None else ""
                )
                out.append(LogEntry(
                    kind=LogEntryKind.TOOL_USE,
                    timestamp=ts,
                    name=name,
                    input=input_str,
                ))
            elif sub in ("function_call_output", "custom_tool_call_output"):
                output = payload.get("output", "")
                content = output if isinstance(output, str) else (
                    json.dumps(output) if output is not None else ""
                )
                if content:
                    out.append(LogEntry(
                        kind=LogEntryKind.TOOL_RESULT,
                        timestamp=ts,
                        content=content,
                    ))
        return out, None

    def interactive_argv(self, session_id: str, config: ActorConfig) -> List[str]:
        # Propagate agent flags so interactive sessions honor the same
        # defaults as non-interactive runs (parity with Claude).
        return [
            "codex", "resume", session_id,
            *self.emit_agent_args(config.agent_args),
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
