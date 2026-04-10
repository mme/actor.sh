# Development Plan: MCP Server + Channels (v2 Stage 1)

## Goal

A working MCP server with channel notifications that lets Claude Code spawn actors and receive results asynchronously. Testable end-to-end with simple commands.

## Scope

**In scope:**
- MCP server (`actor mcp`) with all actor management tools
- Channel capability — push notification into the session when an actor finishes
- Actor subprocess setup so child actors also load the MCP server
- `.mcp.json` generation for actor worktrees
- Testing with `--dangerously-load-development-channels`

**NOT in scope:**
- Plugin packaging (no `.claude-plugin/`, no marketplace)
- Web dashboard
- Status line
- Custom agent (`actor-sh.md`)
- Doctor command
- Actors spawning actors (nested channels) — only top-level session gets notifications
- Permission relay
- SSE/HTTP transport — stdio only

## Current state

- `actor mcp` starts a FastMCP stdio server
- `list_actors` tool works end-to-end
- `run_actor` tool exists but background thread is untested and no channel notification
- No channel capability declared

## Steps

### Step 1: Complete read-only tools

Add to `server.py`:
- `show_actor(name)` — calls `cmd_show`
- `logs_actor(name, verbose)` — calls `cmd_logs`
- `stop_actor(name)` — calls `cmd_stop`
- `discard_actor(name)` — calls `cmd_discard`
- `config_actor(name, pairs)` — calls `cmd_config`

**Test:** Reconnect MCP (`/mcp`), call each tool via conversation.

### Step 2: Fix run_actor background execution

The background thread in `run_actor` needs:
- Proper error handling (log to stderr, update DB on failure)
- A fresh `Database` connection per thread (SQLite connections aren't thread-safe)
- Verify the actor process outlives the thread if the MCP server dies

**Test:**
```
# Via MCP tool: create and run an actor
run_actor(name="test-1", prompt="Create hello.txt with 'hello'", create=True)

# Via CLI: verify it's running
actor list

# Wait for it to finish, then:
actor show test-1
# Should show status=done
```

### Step 3: Add channel capability

Modify `server.py` to:
1. Access the low-level `Server` object from FastMCP
2. Declare `experimental: { 'claude/channel': {} }` capability
3. Set `instructions` string telling Claude what channel events to expect

**Test:**
```bash
# Start claude code with development channels enabled
claude --dangerously-load-development-channels server:actor
```
Verify the server registers as a channel (check startup output).

### Step 4: Push notification on actor completion

When the `run_actor` background thread detects the actor process has exited:
1. Build a summary (actor name, status, exit code)
2. Call `server.send_notification()` with method `notifications/claude/channel`
3. Include actor name and status in `meta`, result summary in `content`

The notification should look like:
```
<channel source="actor" actor="test-1" status="done">
Actor 'test-1' finished successfully.
</channel>
```

**Test:**
```bash
claude --dangerously-load-development-channels server:actor
```
Then in the session:
1. Call `run_actor(name="test-2", prompt="Create hello.txt with 'hello'", create=True)`
2. Tool returns immediately with "Actor 'test-2' is running."
3. Wait a few seconds
4. A channel notification should appear in the session: "Actor 'test-2' finished successfully."
5. Claude reacts to the notification (reads output, reports to user)

### Step 5: Wire up .mcp.json for actor worktrees

When `cmd_new` creates a worktree for an actor, write a `.mcp.json` into it so the actor's Claude session also loads the MCP server:

```json
{
  "mcpServers": {
    "actor": {
      "command": "actor",
      "args": ["mcp"]
    }
  }
}
```

This means actors can call `list_actors`, `show_actor`, etc. to inspect sibling actors.

**Test:**
```bash
# Verify .mcp.json exists in the worktree
cat ~/.actor/worktrees/test-3/.mcp.json
```

### Step 6: End-to-end test

Full flow in a single session:

```bash
claude --dangerously-load-development-channels server:actor
```

1. "Spin up an actor called writer to create a poem in poem.txt"
   → `run_actor` creates actor, returns immediately
2. Wait for channel notification: "Actor 'writer' finished"
3. "What did writer do?"
   → `show_actor` or `logs_actor` shows the result
4. "Show me all actors"
   → `list_actors` shows writer with status=done
5. "Discard writer"
   → `discard_actor` cleans up

## File changes

```
src/actor/server.py     # MCP server with all tools + channel capability
src/actor/commands.py    # Add .mcp.json generation to cmd_new
src/actor/cli.py         # Already has `mcp` subcommand
.mcp.json                # Already exists in project root
```

## Open questions

- How does `--dangerously-load-development-channels server:actor` interact with `.mcp.json`? The server name in `--channels` must match the key in `.mcp.json`. Need to verify.
- Can FastMCP's `Server` object be accessed to declare experimental capabilities and send notifications? Need to check the API.
- If the MCP server process dies mid-actor-run, the actor keeps running but the notification is lost. Acceptable for this stage.
