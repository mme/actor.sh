#!/usr/bin/env python3
"""Drop-in fake for the `codex` CLI used by the e2e suite.

Same shape as `fake_claude.py` but with codex's flag surface and
rollout JSONL format. Behavior knobs use FAKE_CODEX_* env vars.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path


def _ts() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_invocation(argv: list[str], parsed: dict) -> None:
    log_path = os.environ.get("FAKE_CODEX_LOG")
    if not log_path:
        return
    payload = {
        "ts": _ts(),
        "argv": argv,
        "cwd": str(Path.cwd()),
        "env": {
            k: v for k, v in os.environ.items()
            if k.startswith(("ACTOR_", "OPENAI_", "CODEX_"))
        },
        "parsed": parsed,
    }
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(payload) + "\n")


def _parse(argv: list[str]) -> dict:
    parsed: dict = {
        "subcommand": None,         # "exec" / "resume" / None (default = interactive)
        "config": [],               # repeated -c / --config
        "model": None,              # -m / --model
        "approval": None,           # -a / --ask-for-approval
        "sandbox": None,            # -s / --sandbox
        "cd": None,                 # -C / --cd
        "add_dir": [],              # --add-dir (repeated)
        "image": [],                # -i / --image
        "last": False,              # --last (with resume)
        "extra_flags": {},
        "prompt": None,
    }
    i = 0
    after_dashdash = False
    rest_as_prompt: list[str] = []

    if argv and argv[0] in ("exec", "e", "resume", "fork", "review", "login", "logout", "mcp", "plugin", "mcp-server", "app-server", "completion", "update", "sandbox", "debug", "apply", "a", "cloud", "exec-server", "features", "help"):
        parsed["subcommand"] = argv[0]
        i = 1

    while i < len(argv):
        arg = argv[i]
        if after_dashdash:
            rest_as_prompt.append(arg)
            i += 1
            continue
        if arg == "--":
            after_dashdash = True
            i += 1
            continue
        if arg in ("-c", "--config") and i + 1 < len(argv):
            parsed["config"].append(argv[i + 1])
            i += 2
            continue
        if arg in ("-m", "--model") and i + 1 < len(argv):
            parsed["model"] = argv[i + 1]
            i += 2
            continue
        if arg in ("-a", "--ask-for-approval") and i + 1 < len(argv):
            parsed["approval"] = argv[i + 1]
            i += 2
            continue
        if arg in ("-s", "--sandbox") and i + 1 < len(argv):
            parsed["sandbox"] = argv[i + 1]
            i += 2
            continue
        if arg in ("-C", "--cd") and i + 1 < len(argv):
            parsed["cd"] = argv[i + 1]
            i += 2
            continue
        if arg == "--add-dir" and i + 1 < len(argv):
            parsed["add_dir"].append(argv[i + 1])
            i += 2
            continue
        if arg in ("-i", "--image") and i + 1 < len(argv):
            parsed["image"].append(argv[i + 1])
            i += 2
            continue
        if arg == "--last":
            parsed["last"] = True
            i += 1
            continue
        if arg.startswith("-"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                parsed["extra_flags"][arg] = argv[i + 1]
                i += 2
            else:
                parsed["extra_flags"][arg] = True
                i += 1
            continue
        rest_as_prompt.append(arg)
        i += 1

    if rest_as_prompt:
        parsed["prompt"] = " ".join(rest_as_prompt)
    return parsed


def _write_rollout(parsed: dict) -> None:
    """Write a JSONL rollout file in the modern format the codex parser
    in `actor.agents.codex` consumes (per commit bad3193)."""
    home = os.environ.get("HOME")
    if not home:
        return
    rollout_dir_override = os.environ.get("FAKE_CODEX_ROLLOUT_DIR")
    base = Path(rollout_dir_override) if rollout_dir_override else Path(home) / ".codex" / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    log_path = base / f"rollout-{sid}.jsonl"

    response = os.environ.get("FAKE_CODEX_RESPONSE")
    if response is None:
        response = f"[fake codex] received: {parsed['prompt'] or '(no prompt)'}"

    # First emitted line MUST contain `thread_id` — actor.sh's
    # CodexAgent reads stdout's first line to capture the session id
    # (so subsequent runs can `codex exec resume <id>`).
    print(json.dumps({
        "type": "thread.started",
        "thread_id": sid,
        "model": parsed["model"] or "default",
    }))
    sys.stdout.flush()

    # Modern codex rollout format: every record has a top-level
    # `type` and a nested `payload`. actor.sh's CodexAgent parses
    # `event_msg` records by their `payload.type` (agent_message,
    # user_message, token_count); the older flat-shape frames the
    # fake used to emit don't make it into `actor logs`.
    frames: list[dict] = []
    frames.append({
        "type": "session_meta",
        "payload": {
            "session_id": sid,
            "model": parsed["model"] or "default",
        },
        "timestamp": _ts(),
    })
    if parsed["prompt"]:
        frames.append({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": parsed["prompt"]},
            "timestamp": _ts(),
        })
    tools_json = os.environ.get("FAKE_CODEX_TOOLS")
    if tools_json:
        try:
            tool_calls = json.loads(tools_json)
        except json.JSONDecodeError:
            tool_calls = []
        for call in tool_calls:
            frames.append({
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": call.get("id", str(uuid.uuid4())),
                    "name": call.get("name", "shell"),
                    "arguments": json.dumps(call.get("args", {})),
                },
                "timestamp": _ts(),
            })
            frames.append({
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call.get("id", ""),
                    "output": call.get("result", "ok"),
                },
                "timestamp": _ts(),
            })
    reasoning = os.environ.get("FAKE_CODEX_REASONING")
    if reasoning:
        frames.append({
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [{"text": reasoning}],
            },
            "timestamp": _ts(),
        })
    frames.append({
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": response},
        "timestamp": _ts(),
    })
    frames.append({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 100,
                    "output_tokens": len(response),
                },
            },
        },
        "timestamp": _ts(),
    })

    with open(log_path, "a") as f:
        for frame in frames:
            f.write(json.dumps(frame) + "\n")

    # actor.sh's codex log reader looks the rollout path up in
    # `~/.codex/state_5.sqlite` keyed by thread_id. Real codex
    # maintains this DB; the fake populates it so the parser can find
    # the rollout we just wrote.
    state_db = Path(home) / ".codex" / "state_5.sqlite"
    state_db.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(state_db), timeout=10.0)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT OR REPLACE INTO threads (id, rollout_path) VALUES (?, ?)",
            (sid, str(log_path)),
        )
        conn.commit()
    finally:
        conn.close()

    # actor.sh's CodexAgent relay reads `item.completed` events with
    # an `agent_message` item. Emit one so the actor's output appears
    # in `actor logs`.
    print(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": response},
    }))
    sys.stdout.flush()


def _maybe_crash() -> None:
    crash = os.environ.get("FAKE_CODEX_CRASH")
    if not crash:
        return
    try:
        sig = getattr(signal, crash if crash.startswith("SIG") else f"SIG{crash}")
    except AttributeError:
        sig = signal.SIGKILL
    os.kill(os.getpid(), sig)


def _maybe_spawn_child() -> None:
    cmd = os.environ.get("FAKE_CODEX_SPAWN_CHILD")
    if not cmd:
        return
    import subprocess
    subprocess.Popen(["/bin/sh", "-c", cmd], start_new_session=True)


def main() -> None:
    argv = sys.argv[1:]
    parsed = _parse(argv)
    _record_invocation(argv, parsed)

    sleep_for = float(os.environ.get("FAKE_CODEX_SLEEP", "0") or 0)
    if sleep_for > 0:
        time.sleep(sleep_for)

    _maybe_crash()

    _write_rollout(parsed)
    _maybe_spawn_child()

    exit_code = int(os.environ.get("FAKE_CODEX_EXIT", "0") or 0)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
