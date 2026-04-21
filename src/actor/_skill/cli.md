# Actor CLI Reference (fallback)

This file documents the `actor` command-line interface. **If you have access to `mcp__actor__*` tools, use those instead** — see [SKILL.md](SKILL.md). The MCP server returns immediately and emits a channel notification when each actor finishes, giving you an async-notification workflow. The CLI has none of that: there's no push-completion, no structured arguments, and you lose the main reason the skill exists.

Use this reference only when:
- The MCP server genuinely can't be installed in the user's environment, and
- The user has explicitly opted out of installing it after being offered.

For Codex users: actor.sh is not currently compatible with Codex regardless of path, because Codex doesn't forward MCP notifications into the model's conversation. See [SKILL.md](SKILL.md) § Agent compatibility.

## Prerequisite: install `actor`

If `actor --version` fails, the CLI (and the MCP server) isn't installed. Install with:

```
uv tool install actor-sh
```

or:

```
pip install actor-sh
```

---

## Create and run an actor

Pass a prompt to create and run in one step.

```bash
actor new fix-nav "Fix the nav bar — broken on mobile"
actor new fix-nav --agent codex "..."                                       # Codex actor
actor new fix-nav --base develop "..."                                      # branch off develop
actor new fix-nav --dir /path/to/repo "..."                                 # worktree from another repo
actor new fix-nav --no-worktree "..."                                       # no worktree
actor new fix-nav --config model=opus "..."                                 # saved defaults
actor new fix-nav --no-use-subscription "..."                               # pass API keys through
actor new fix-nav --template qa                                             # apply a template from settings.kdl
echo "fix it" | actor new fix-nav                                           # prompt from stdin
```

Templates come from `~/.actor/settings.kdl` (user) or
`<repo>/.actor/settings.kdl` (project-local; project wins on overlap).
These files don't exist by default — create them by hand when the user
wants a template. A template can set the agent, prompt, and any config
keys:

```kdl
template "qa" {
    agent "claude"
    model "opus"
    prompt "You're a QA engineer. Write tests for the changed code."
}
```

Any explicit flag on the CLI (`--agent`, `--model`, `--config`, positional
prompt / stdin) beats the template's value.

## Create without running

```bash
actor new fix-nav
```

## Run an existing actor

```bash
actor run fix-nav "continue fixing"
actor run fix-nav --config model=opus "..."                                 # per-run override (not saved)
echo "fix it" | actor run fix-nav                                           # prompt from stdin
actor run fix-nav -i                                                        # resume interactively in the current TTY
```

`-i` (interactive) drops you into a live Claude/Codex session that resumes
the actor's prior conversation. Tracked as a Run with prompt `*interactive*`
so it shows up in `actor show` / the watch dashboard alongside normal runs.

## Change actor configuration

```bash
actor config fix-nav                                                        # view
actor config fix-nav model=opus                                             # update
actor config fix-nav model=sonnet effort=max                                # multiple at once
```

Config reference:
- [Claude config](claude-config.md)
- [Codex config](codex-config.md)

## Monitor

```bash
actor list
actor list --status running
actor show fix-nav
actor show fix-nav --runs 20
actor logs fix-nav
actor logs fix-nav --verbose
actor logs fix-nav --watch                                                  # stream (CLI-only)
```

## Stop / discard

```bash
actor stop fix-nav
actor discard fix-nav
```

## Background execution

Because the CLI doesn't push completion events back to the model, running an actor in the CLI is inherently a foreground operation from the model's perspective. If your host supports background subprocesses (e.g., Claude Code's Bash tool `run_in_background: true`), use it — each `actor run` / `actor new <with prompt>` MUST be its own separate tool call. Do not use shell `&`, and never batch multiple runs in one call.

Even with `run_in_background`, you'll only learn an actor finished when the host delivers the subprocess-exit notification. That's strictly inferior to the MCP channel notification, which carries the actor's actual output inline. This is why the MCP path is the recommended one.
