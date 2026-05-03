"""Helpers for setting per-call behavior of the fake claude / codex.

The fakes read FAKE_CLAUDE_* / FAKE_CODEX_* env vars at startup. These
helpers package the most common knobs into context-manager-style dicts
that the test author layers onto `env.run_cli(...)` or other env-aware
calls.

Usage:

    with isolated_home() as env:
        env.run_cli(["new", "alice", "do thing"], **claude_responds(
            "Done", sleep=0.1, exit=0,
        ))
"""
from __future__ import annotations


def claude_responds(
    text: str | None = None,
    *,
    sleep: float = 0.0,
    exit: int = 0,
    crash: str | None = None,
    response_file: str | None = None,
    thinking: str | None = None,
    tools: list[dict] | None = None,
    spawn_child: str | None = None,
    log_dir: str | None = None,
) -> dict[str, str]:
    """Build an env-overrides dict for fake claude. Pass to
    `env.run_cli(..., **claude_responds(...))`."""
    import json
    overrides: dict[str, str] = {}
    if text is not None:
        overrides["FAKE_CLAUDE_RESPONSE"] = text
    if response_file is not None:
        overrides["FAKE_CLAUDE_RESPONSE_FILE"] = response_file
    if sleep:
        overrides["FAKE_CLAUDE_SLEEP"] = str(sleep)
    if exit != 0:
        overrides["FAKE_CLAUDE_EXIT"] = str(exit)
    if crash:
        overrides["FAKE_CLAUDE_CRASH"] = crash
    if thinking:
        overrides["FAKE_CLAUDE_THINKING"] = thinking
    if tools:
        overrides["FAKE_CLAUDE_TOOLS"] = json.dumps(tools)
    if spawn_child:
        overrides["FAKE_CLAUDE_SPAWN_CHILD"] = spawn_child
    if log_dir:
        overrides["FAKE_CLAUDE_LOG_DIR"] = log_dir
    return overrides


def codex_responds(
    text: str | None = None,
    *,
    sleep: float = 0.0,
    exit: int = 0,
    crash: str | None = None,
    reasoning: str | None = None,
    tools: list[dict] | None = None,
    spawn_child: str | None = None,
    rollout_dir: str | None = None,
) -> dict[str, str]:
    import json
    overrides: dict[str, str] = {}
    if text is not None:
        overrides["FAKE_CODEX_RESPONSE"] = text
    if sleep:
        overrides["FAKE_CODEX_SLEEP"] = str(sleep)
    if exit != 0:
        overrides["FAKE_CODEX_EXIT"] = str(exit)
    if crash:
        overrides["FAKE_CODEX_CRASH"] = crash
    if reasoning:
        overrides["FAKE_CODEX_REASONING"] = reasoning
    if tools:
        overrides["FAKE_CODEX_TOOLS"] = json.dumps(tools)
    if spawn_child:
        overrides["FAKE_CODEX_SPAWN_CHILD"] = spawn_child
    if rollout_dir:
        overrides["FAKE_CODEX_ROLLOUT_DIR"] = rollout_dir
    return overrides
