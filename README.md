# actor.sh

A Master Orchestrator session backed by parallel coding sub-agents. You
talk to one Claude session — `actor main` — and it spawns specialized
sub-actors in isolated git worktrees to handle the actual work,
verifies their output, and reports back.

Sub-agents can be Claude Code or Codex. Each runs in its own worktree
on its own branch, so parallel work doesn't collide. Completion is
pushed back to the orchestrator via channel notifications, so it knows
when work is done without polling.

## Setup

```bash
# Install uv (if needed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install actor-sh
uv tool install actor-sh            # or: pip install actor-sh

# Register the Claude Code skill + MCP server
actor setup --for claude-code       # user-wide
# or: actor setup --for claude-code --scope project   # project-local
```

## First run

```bash
cd <your repo>
actor main
```

`actor main` launches Claude Code with the built-in `main` role's
system prompt (the Master Orchestrator brief) and the actor channel
enabled, so completion notifications from sub-actors flow back into
the conversation.

From there, talk to it like a project lead:

> "Spin up an actor to refactor the auth module — also kick off a
> reviewer to look at the API once that's done."

The orchestrator decides what to spawn, manages it via MCP, verifies
the work, and tells you when there's something for you to look at.

## Roles

A role is a named preset for sub-actors — agent + system prompt +
config. The built-in `main` role is the Master Orchestrator (always
present). To define your own, drop a block in
`~/.actor/settings.kdl` (user-wide) or `<repo>/.actor/settings.kdl`
(project-local):

```kdl
role "reviewer" {
    description "Concise code review; flag bugs and style issues."
    agent "claude"
    model "opus"
    prompt "You are a senior code reviewer. Be concise; flag bugs, security issues, and style violations. Don't fix anything — report findings only."
}

role "designer" {
    description "Design-led actor for UX and visual decisions."
    agent "claude"
    prompt "You are a senior product designer. Propose 2-3 options, pick one with reasoning."
}
```

Apply roles via the orchestrator (which calls
`mcp__actor__list_roles` to discover them) or directly:

```bash
actor roles                                          # see what's defined
actor new auth-review --role reviewer "Review src/auth/*.py"
```

## Updating

After bumping `actor-sh` to a new version, refresh the deployed skill:

```bash
actor update
```

## Running tests

```bash
uv run python -m unittest discover tests
```

## More

- Orchestrator behavior: see `src/actor/role_prompts/main.md`
- Skill / tool reference: see `src/actor/_skill/SKILL.md`
- Project conventions: see `CLAUDE.md`
