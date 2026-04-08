---
name: direct
description: Run an actor command in the foreground and wait for completion. Use instead of the actor skill when you need the result before proceeding.
argument-hint: [actor-name] <prompt or question>
disable-model-invocation: true
allowed-tools: Bash(*/actor.sh *)
---

# Direct — Foreground Actor Execution

The user said: $ARGUMENTS

Interpret what they want and run the appropriate `actor run` command. Use conversation context to determine which actor they mean, whether to create one, and what prompt to send.

## Setup

```!
echo "$(dirname "${CLAUDE_SKILL_DIR}")/actor/actor.sh"
```

Refer to this as `ACTOR`.

## Execution

Run `ACTOR run` in the **foreground**. Do NOT use `run_in_background`. Set timeout to 600000. Wait for the response and relay it to the user.
