#!/usr/bin/env python3
"""Drop-in fake for the `claude` CLI used by the e2e suite.

Behavior is controlled by env vars set by the test harness — see
`e2e/harness/fakes_control.py`. The fake:

- Accepts the flag subset actor.sh actually passes (any unknown flag
  is recorded but doesn't crash, so flag drift surfaces in tests as
  assertions on the recorded invocation, not as fake failures).
- Writes a JSONL session log to
  `$HOME/.claude/projects/<encoded-cwd>/<session-id>.jsonl` matching
  what `actor.agents.claude.ClaudeAgent._parse_log_dict` expects.
- Echoes the prompt by default; respond with FAKE_CLAUDE_RESPONSE
  to control the assistant text.
- Sleeps FAKE_CLAUDE_SLEEP seconds before exiting.
- Exits with FAKE_CLAUDE_EXIT (default 0).
- Records the full invocation to FAKE_CLAUDE_LOG (a json-lines file
  the harness reads to assert which flags / values were forwarded).

Avoids any actor.sh imports — this script runs as a child process
of the actual `actor` CLI under test, and must work even if our
package is broken.
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


def _encoded_dir(p: Path) -> str:
    """Match actor.agents.claude.ClaudeAgent._encode_dir."""
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in str(p))


def _ts() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_invocation(argv: list[str], parsed: dict) -> None:
    """Append the invocation to FAKE_CLAUDE_LOG (jsonl) so the harness
    can assert on flags after the fact."""
    log_path = os.environ.get("FAKE_CLAUDE_LOG")
    if not log_path:
        return
    payload = {
        "ts": _ts(),
        "argv": argv,
        "cwd": str(Path.cwd()),
        "env": {
            k: v for k, v in os.environ.items()
            if k.startswith(("ACTOR_", "ANTHROPIC_", "CLAUDE_"))
        },
        "parsed": parsed,
    }
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(payload) + "\n")


def _parse(argv: list[str]) -> dict:
    """Parse the well-known subset of claude flags that actor.sh emits.

    Unknown flags are recorded under `extra_flags` so tests can detect
    drift. Positional prompt comes after `--` or as the last
    standalone argument."""
    parsed = {
        "channel_flag": False,
        "session_id": None,
        "resume": None,
        "continue": False,
        "system_prompt": None,
        "append_system_prompt": None,
        "model": None,
        "permission_mode": None,
        "config": [],
        "extra_flags": {},
        "prompt": None,
        "print_mode": False,
    }
    i = 0
    after_dashdash = False
    rest_as_prompt: list[str] = []
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
        if arg == "--dangerously-load-development-channels":
            parsed["channel_flag"] = True
            # consumes one value (e.g. "server:actor")
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                i += 2
            else:
                i += 1
            continue
        if arg == "-p":
            parsed["print_mode"] = True
            i += 1
            continue
        if arg in ("-c", "--continue"):
            parsed["continue"] = True
            i += 1
            continue
        if arg == "--session-id" and i + 1 < len(argv):
            parsed["session_id"] = argv[i + 1]
            i += 2
            continue
        if arg == "--resume" and i + 1 < len(argv):
            parsed["resume"] = argv[i + 1]
            i += 2
            continue
        if arg == "--system-prompt" and i + 1 < len(argv):
            parsed["system_prompt"] = argv[i + 1]
            i += 2
            continue
        if arg == "--append-system-prompt" and i + 1 < len(argv):
            parsed["append_system_prompt"] = argv[i + 1]
            i += 2
            continue
        if arg == "--model" and i + 1 < len(argv):
            parsed["model"] = argv[i + 1]
            i += 2
            continue
        if arg == "--permission-mode" and i + 1 < len(argv):
            parsed["permission_mode"] = argv[i + 1]
            i += 2
            continue
        if arg == "--config" and i + 1 < len(argv):
            parsed["config"].append(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--"):
            # Unknown long flag; record key + (maybe) value
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                parsed["extra_flags"][arg] = argv[i + 1]
                i += 2
            else:
                parsed["extra_flags"][arg] = True
                i += 1
            continue
        # Positional: treat as the prompt (last positional wins)
        rest_as_prompt.append(arg)
        i += 1

    if rest_as_prompt:
        parsed["prompt"] = " ".join(rest_as_prompt)
    return parsed


def _write_session_log(parsed: dict) -> None:
    """Write a JSONL session log that ClaudeAgent's parser can read."""
    home = os.environ.get("HOME")
    if not home:
        return
    log_dir_override = os.environ.get("FAKE_CLAUDE_LOG_DIR")
    cwd = Path.cwd()
    encoded = _encoded_dir(cwd)
    base = Path(log_dir_override) if log_dir_override else Path(home) / ".claude" / "projects" / encoded
    base.mkdir(parents=True, exist_ok=True)
    sid = parsed["session_id"] or parsed["resume"] or str(uuid.uuid4())
    log_path = base / f"{sid}.jsonl"

    response = os.environ.get("FAKE_CLAUDE_RESPONSE")
    if response is None:
        # Default: a small acknowledgement that includes the prompt so
        # tests can assert the prompt round-tripped correctly.
        response = f"[fake claude] received: {parsed['prompt'] or '(no prompt)'}"
    response_file = os.environ.get("FAKE_CLAUDE_RESPONSE_FILE")
    if response_file and Path(response_file).is_file():
        response = Path(response_file).read_text()

    frames: list[dict] = []
    # Resume mode: only append the new turn (don't re-emit the system frame).
    if not parsed["resume"]:
        frames.append({
            "type": "system",
            "message": {"text": "Session started"},
            "timestamp": _ts(),
        })
    if parsed["prompt"]:
        frames.append({
            "type": "user",
            "message": {"content": parsed["prompt"]},
            "timestamp": _ts(),
        })
    # Optional thinking frame.
    thinking = os.environ.get("FAKE_CLAUDE_THINKING")
    if thinking:
        frames.append({
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": thinking}],
            },
            "timestamp": _ts(),
        })
    # Optional tool calls.
    tools_json = os.environ.get("FAKE_CLAUDE_TOOLS")
    if tools_json:
        try:
            tool_calls = json.loads(tools_json)
        except json.JSONDecodeError:
            tool_calls = []
        for call in tool_calls:
            frames.append({
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": call.get("id", str(uuid.uuid4())),
                        "name": call.get("name", "Bash"),
                        "input": call.get("input", {}),
                    }],
                },
                "timestamp": _ts(),
            })
            frames.append({
                "type": "user",
                "message": {
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": call.get("id", ""),
                        "content": call.get("result", "ok"),
                    }],
                },
                "timestamp": _ts(),
            })
    frames.append({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": response}]},
        "timestamp": _ts(),
    })

    with open(log_path, "a") as f:
        for frame in frames:
            f.write(json.dumps(frame) + "\n")

    # Also print the response to stdout (matches `claude -p` behavior).
    if parsed["print_mode"]:
        print(response)


def _maybe_spawn_child() -> None:
    cmd = os.environ.get("FAKE_CLAUDE_SPAWN_CHILD")
    if not cmd:
        return
    import subprocess
    # Detached so the child outlives this process.
    subprocess.Popen(["/bin/sh", "-c", cmd], start_new_session=True)


def _maybe_crash() -> None:
    crash = os.environ.get("FAKE_CLAUDE_CRASH")
    if not crash:
        return
    try:
        sig = getattr(signal, crash if crash.startswith("SIG") else f"SIG{crash}")
    except AttributeError:
        sig = signal.SIGKILL
    os.kill(os.getpid(), sig)


def _maybe_run_interactive() -> None:
    """If FAKE_CLAUDE_INTERACTIVE=1 is set, behave like a tiny echo
    shell: read bytes off stdin and echo them on stdout, exit on
    EOF or on the byte sequence FAKE_CLAUDE_INTERACTIVE_QUIT (default
    'q\\n'). Used by Phase 2.5 InteractiveSession tests to exercise
    the daemon-side PTY without needing a real claude binary.
    """
    if not os.environ.get("FAKE_CLAUDE_INTERACTIVE"):
        return
    quit_marker = os.environ.get(
        "FAKE_CLAUDE_INTERACTIVE_QUIT", "q\n",
    ).encode("utf-8", errors="replace")
    sys.stdout.write("[fake claude interactive] ready\n")
    sys.stdout.flush()
    buf = b""
    while True:
        try:
            chunk = os.read(0, 1024)
        except OSError:
            break
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        buf += chunk
        if quit_marker and quit_marker in buf:
            break
    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0") or 0))


def main() -> None:
    argv = sys.argv[1:]
    parsed = _parse(argv)
    _record_invocation(argv, parsed)

    sleep_for = float(os.environ.get("FAKE_CLAUDE_SLEEP", "0") or 0)
    if sleep_for > 0:
        time.sleep(sleep_for)

    _maybe_crash()
    _maybe_run_interactive()

    _write_session_log(parsed)
    _maybe_spawn_child()

    exit_code = int(os.environ.get("FAKE_CLAUDE_EXIT", "0") or 0)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
