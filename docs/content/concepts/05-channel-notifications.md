---
title: "Channel notifications"
description: "How actor completion events flow back into the orchestrator's conversation."
weight: 5
slug: "channel-notifications"
---

When you spawn an actor with `new_actor` or `run_actor`, the MCP tool returns immediately — the agent runs in a background thread. The orchestrator doesn't poll for completion; it gets a push notification when each actor finishes. That mechanism is the **actor channel**.

## The capability

The actor MCP server declares an experimental capability called `claude/channel` during its initialization handshake. When an actor's run finishes, the server emits a `notifications/claude/channel` JSON-RPC notification through the same MCP session that started the run:

```python
# src/actor/server.py
notification = JSONRPCNotification(
    jsonrpc="2.0",
    method="notifications/claude/channel",
    params={"content": f"[{name}] {body}", "meta": {"actor": name, "status": status}},
)
```

The `meta` dict carries the actor name and final status (`done`, `error`, `stopped`, or the special string `discarded` if the actor row was removed before the run could record a status). The orchestrator receives the event as a `<channel source="actor" ...>` block in its conversation and reads the result inline.

## Claude Code only — for now

This wiring depends on the host forwarding custom MCP notifications into the model's conversation. Today, that's Claude Code.

Codex does **not** forward MCP server notifications to the model — tracked in [openai/codex#17543](https://github.com/openai/codex/issues/17543) and [#18056](https://github.com/openai/codex/issues/18056). Codex actors still run, but a Codex orchestrator never learns when they finish; it would have to poll with `list_actors` or `show_actor` instead. For now, run your orchestrator on Claude Code; Codex is fine as an actor agent (`agent="codex"`), just not as the orchestrator.

## Each spawn must be its own tool call

There is one mechanical rule for using these tools: **each `new_actor` or `run_actor` MUST be its own tool call.** Never batch multiple in a single combined tool use. Completion notifications are routed by session; batching them in one call mis-routes the events and the orchestrator loses track of which actor finished.

This is why the skill instructions tell the orchestrator to spawn three actors as three separate tool uses, not one.

## Sub-claudes inherit the channel

Actors are themselves Claude sessions, and they can spawn their own actors. To make nested orchestration work, `ClaudeAgent` automatically forwards the channel flag to every sub-claude it launches:

```python
# src/actor/agents/claude.py
_CHANNEL_ARGS = ["--dangerously-load-development-channels", "server:actor"]
```

Both `start` and `resume` prepend these args to the `claude` invocation, so a child actor's session sees the same `claude/channel` capability as the top-level orchestrator. Grandchild actors then notify their parents, recursively.

## CLI fallback has no notifications

`actor new` and `actor run` from a regular shell (no MCP) work the same way — they spawn the agent in the background and return — but there's no push channel back to anywhere. You'd have to `actor show <name>` or `actor logs <name>` to see whether the actor finished. The CLI is a strict subset of the MCP path; for orchestrated work, use `actor main` and let the channel notifications drive the conversation.
