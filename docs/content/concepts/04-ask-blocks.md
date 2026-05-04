---
title: "Ask blocks"
description: "Customize when the orchestrator asks the user before spawning, running, or discarding actors."
weight: 4
slug: "ask-blocks"
---

The `ask { }` block in `settings.kdl` lets you tune the orchestrator's "should I ask the user a question first?" behavior for the three lifecycle MCP tools that take meaningful parameters: `new_actor`, `run_actor`, and `discard_actor`. The strings you write are appended to the tools' descriptions at MCP-server startup, so they show up directly in the orchestrator's tool catalog.

## Shape

```kdl
ask {
    on-start   "Always confirm the agent kind for risky tasks."
    before-run "Skip questions; assume per-run config never changes."
    on-discard null
}
```

Three keys are recognized — and only three:

- `on-start` — appended to the description of `new_actor`.
- `before-run` — appended to the description of `run_actor`.
- `on-discard` — appended to the description of `discard_actor`.

There is intentionally **no** `after-run` — the run has already finished, there's nothing for the orchestrator to ask before doing.

## Per-key resolution

For each key, the lookup is:

- **Key absent** — fall through to the hardcoded default (the orchestrator's baseline guidance for that tool).
- **String value** — append that string verbatim to the tool description.
- **`null` or `""`** — opt out. Append nothing; the tool's description has no behavioral guidance about when to ask.

This means you can both tighten the defaults (give the orchestrator stricter rules) and loosen them (silence the default with `null` for tools where you want the model to act without asking).

## What the defaults look like

The hardcoded defaults live in `ASK_DEFAULTS` in `src/actor/config.py`. The `on-start` default reads, in part:

> Before calling `new_actor`, use `AskUserQuestion` to surface any parameter choices that would meaningfully affect the actor's behavior. […] Only ask when the user's request leaves the choice genuinely ambiguous — skip questions whose answer is already clear from context or whose default is fine.

The `before-run` default tells the orchestrator to default to proceeding without asking unless the prompt is genuinely vague or the user signals per-run config tweaks. The `on-discard` default tells it to ask only when the target is ambiguous or there's a running session worth inspecting first.

In each case, the user can write a string to override and steer in either direction.

## When edits take effect

Tool descriptions are computed once at MCP-server startup and stay static for the server's lifetime. After editing `settings.kdl`, re-exec the orchestrator (`actor main`) so the new descriptions are picked up. There's no hot reload.

## Precedence

User and project `ask` blocks merge **per key**, not per block. A project file that sets only `before-run` leaves the user's `on-start` and `on-discard` intact; a project file that sets `on-start null` silences whatever the user (or the default) had there.

See [hooks](../03-hooks/) for the related `hooks { }` block, which uses the same `on-start` / `before-run` / `on-discard` vocabulary but for shell commands rather than orchestrator guidance.
