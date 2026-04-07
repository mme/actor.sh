from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from .errors import (
    ActorError,
    AgentNotFoundError,
    IsRunningError,
    NotRunningError,
)
from .interfaces import Agent, GitOps, LogEntry, LogEntryKind, ProcessManager, binary_exists
from .types import (
    Actor,
    AgentKind,
    Config,
    Run,
    Status,
    _now_iso,
    _parse_iso,
    _sorted_config,
    parse_config,
    validate_name,
)
from .db import Database
from .agents.claude import ClaudeAgent
from .agents.codex import CodexAgent


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _home_dir() -> Path:
    home = os.environ.get("HOME", "")
    if not home:
        raise ActorError("HOME environment variable is not set")
    return Path(home)


def _worktree_path(name: str) -> Path:
    return _home_dir() / ".actor" / "worktrees" / name


def _truncate(s: str, max_len: int) -> str:
    """Truncate to first line and max chars, appending ... if longer."""
    first_line = s.split("\n", 1)[0]
    # Use character count for multibyte safety
    chars = list(first_line)
    if len(chars) <= max_len:
        return first_line
    return "".join(chars[:max_len]) + "..."


def _format_duration(started_at: str, finished_at: Optional[str]) -> str:
    """Format a duration between two ISO timestamps as 'Xm Ys'."""
    if finished_at is None:
        return "\u2014"
    start = _parse_iso(started_at)
    end = _parse_iso(finished_at)
    if start is None or end is None:
        return "\u2014"
    secs = max(0, int((end - start).total_seconds()))
    minutes = secs // 60
    remainder = secs % 60
    if minutes > 0:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def _create_agent(kind: AgentKind) -> Agent:
    if kind == AgentKind.CLAUDE:
        return ClaudeAgent()
    elif kind == AgentKind.CODEX:
        return CodexAgent()
    else:
        raise ActorError(f"unknown agent kind: {kind}")


def _format_log_timestamp(ts: Optional[str]) -> str:
    if ts is None:
        return ""
    dt = _parse_iso(ts)
    if dt is not None:
        return f"[{dt.strftime('%Y-%m-%d %H:%M:%S')}] "
    return f"[{ts}] "


def _truncate_input(input_str: str, max_len: int) -> str:
    if len(input_str) <= max_len:
        return input_str
    return input_str[:max_len] + "..."


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# -- cmd_new --

def cmd_new(
    db: Database,
    git: GitOps,
    name: str,
    dir: Optional[str],
    no_worktree: bool,
    base: Optional[str],
    agent_name: str,
    config_pairs: List[str],
) -> Actor:
    validate_name(name)

    agent_kind = AgentKind.from_str(agent_name)

    if not binary_exists(agent_kind.binary_name):
        print(f"warning: '{agent_kind.binary_name}' not found on PATH", file=sys.stderr)

    config = parse_config(config_pairs)

    if dir is not None:
        try:
            base_dir = Path(dir).resolve(strict=True)
        except (OSError, ValueError) as e:
            raise ActorError(f"cannot resolve --dir: {e}")
    else:
        base_dir = Path.cwd()

    use_worktree = (not no_worktree) and git.is_repo(base_dir)

    if not use_worktree:
        actor_dir = str(base_dir)
        source_repo = None
        base_branch = None
        worktree = False
    else:
        if base is not None:
            branch_base = base
        else:
            branch_base = git.current_branch(base_dir)

        wt_path = _worktree_path(name)
        git.create_worktree(base_dir, wt_path, name, branch_base)

        actor_dir = str(wt_path)
        source_repo = str(base_dir)
        base_branch = branch_base
        worktree = True

    now = _now_iso()
    actor = Actor(
        name=name,
        agent=agent_kind,
        agent_session=None,
        dir=actor_dir,
        source_repo=source_repo,
        base_branch=base_branch,
        worktree=worktree,
        config=config,
        created_at=now,
        updated_at=now,
    )

    try:
        db.insert_actor(actor)
    except ActorError:
        if worktree:
            wt_path = _worktree_path(name)
            try:
                git.remove_worktree(Path(source_repo), wt_path)  # type: ignore[arg-type]
            except Exception as cleanup_err:
                print(f"warning: failed to clean up worktree at {wt_path}: {cleanup_err}", file=sys.stderr)
        raise

    return actor


# -- cmd_run --

def cmd_run(
    db: Database,
    agent: Agent,
    proc_mgr: ProcessManager,
    name: str,
    prompt: str,
    config_pairs: List[str],
) -> None:
    actor = db.get_actor(name)

    # Check if already running (with stale PID detection)
    status = db.resolve_actor_status(name, proc_mgr)
    if status == Status.RUNNING:
        raise IsRunningError(name)

    # Check agent binary exists
    if not binary_exists(actor.agent.binary_name):
        raise AgentNotFoundError(actor.agent.binary_name)

    # Check actor directory exists
    dir_path = Path(actor.dir)
    if not dir_path.is_dir():
        raise ActorError(
            f"actor directory '{actor.dir}' does not exist \u2014 use 'actor done {name}' to clean up"
        )

    # Merge config: actor defaults + run overrides
    run_overrides = parse_config(config_pairs)
    effective_config = dict(actor.config)
    for k, v in run_overrides.items():
        effective_config[k] = v
    effective_config = _sorted_config(effective_config)

    dir_p = Path(actor.dir)

    # Insert run row BEFORE starting agent so list/show see it immediately
    now = _now_iso()
    run = Run(
        id=0,
        actor_name=name,
        prompt=prompt,
        status=Status.RUNNING,
        exit_code=None,
        pid=None,
        config=effective_config,
        started_at=now,
        finished_at=None,
    )
    run_id = db.insert_run(run)

    # Start or resume
    try:
        if actor.agent_session is not None:
            pid = agent.resume(dir_p, actor.agent_session, prompt, effective_config)
            new_session: Optional[str] = None
        else:
            pid, new_session = agent.start(dir_p, prompt, effective_config)
    except Exception:
        # Agent failed to start — mark run as error
        db.update_run_status(run_id, Status.ERROR, -1)
        raise

    # Update PID on the run row
    db.update_run_pid(run_id, pid)

    # Update session ID if we got one
    if new_session is not None:
        db.update_actor_session(name, new_session)

    # Block until agent exits
    exit_code = agent.wait(pid)

    # Check if stop command already updated this run (race condition)
    current_run = db.latest_run(name)
    if current_run is not None and current_run.id == run_id and current_run.status == Status.STOPPED:
        return

    status = Status.DONE if exit_code == 0 else Status.ERROR
    db.update_run_status(run_id, status, exit_code)


# -- cmd_list --

def cmd_list(db: Database, pm: ProcessManager, status_filter: Optional[str]) -> str:
    actors = db.list_actors()

    # Validate the status filter early
    filter_status: Optional[Status] = None
    if status_filter is not None:
        filter_status = Status.from_str(status_filter)

    rows: List[Tuple[str, Status, str]] = []  # (name, status, prompt)

    for actor in actors:
        status = db.resolve_actor_status(actor.name, pm)

        if filter_status is not None and status != filter_status:
            continue

        latest = db.latest_run(actor.name)
        prompt = _truncate(latest.prompt, 40) if latest is not None else ""

        rows.append((actor.name, status, prompt))

    # Build table output
    h_name = "NAME"
    h_status = "STATUS"
    h_prompt = "PROMPT"

    name_width = max((len(r[0]) for r in rows), default=0)
    name_width = max(name_width, len(h_name))
    status_width = max((len(r[1].as_str()) for r in rows), default=0)
    status_width = max(status_width, len(h_status))

    output = f"{h_name:<{name_width}}  {h_status:<{status_width}}  {h_prompt}\n"

    for name, st, prompt in rows:
        output += f"{name:<{name_width}}  {st.as_str():<{status_width}}  {prompt}\n"

    return output


# -- cmd_show --

def cmd_show(db: Database, pm: ProcessManager, name: str, runs_limit: int) -> str:
    actor = db.get_actor(name)

    # Derive status with stale PID detection
    status = db.resolve_actor_status(actor.name, pm)

    output = ""
    output += f"Name:      {actor.name}\n"
    output += f"Agent:     {actor.agent}\n"
    output += f"Status:    {status}\n"
    output += f"Dir:       {actor.dir}\n"

    if actor.base_branch is not None:
        output += f"Base:      {actor.base_branch}\n"

    if actor.config:
        pairs = [f"{k}={v}" for k, v in sorted(actor.config.items())]
        output += f"Config:    {', '.join(pairs)}\n"

    if actor.agent_session is not None:
        output += f"Session:   {actor.agent_session}\n"

    output += f"Created:   {actor.created_at}\n"

    # Runs section
    if runs_limit == 0:
        return output

    runs, total = db.list_runs(actor.name, runs_limit)

    output += "\n"

    if not runs:
        output += "No runs yet.\n"
        return output

    # Build runs table
    run_rows: List[Tuple[str, str, str, str, str]] = []
    for r in runs:
        run_rows.append((
            str(r.id),
            r.status.as_str(),
            _truncate(r.prompt, 40),
            _format_duration(r.started_at, r.finished_at),
            str(r.exit_code) if r.exit_code is not None else "\u2014",
        ))

    h_run = "RUN"
    h_status = "STATUS"
    h_prompt = "PROMPT"
    h_duration = "DURATION"
    h_exit = "EXIT"

    w_run = max(max(len(r[0]) for r in run_rows), len(h_run))
    w_status = max(max(len(r[1]) for r in run_rows), len(h_status))
    w_prompt = max(max(len(r[2]) for r in run_rows), len(h_prompt))
    w_duration = max(max(len(r[3]) for r in run_rows), len(h_duration))

    output += (
        f"{h_run:<{w_run}}  {h_status:<{w_status}}  "
        f"{h_prompt:<{w_prompt}}  {h_duration:<{w_duration}}  {h_exit}\n"
    )

    for rid, st, pr, dur, ex in run_rows:
        output += (
            f"{rid:<{w_run}}  {st:<{w_status}}  "
            f"{pr:<{w_prompt}}  {dur:<{w_duration}}  {ex}\n"
        )

    if total > runs_limit:
        output += f"\n{total} total runs \u2014 use --runs to show more\n"

    return output


# -- cmd_stop --

def cmd_stop(
    db: Database,
    agent: Agent,
    proc_mgr: ProcessManager,
    name: str,
) -> str:
    # Verify the actor exists
    _actor = db.get_actor(name)

    # Get the latest run
    latest = db.latest_run(name)
    if latest is None:
        raise NotRunningError(name)

    # Must be running
    if latest.status != Status.RUNNING:
        raise NotRunningError(name)

    pid = latest.pid

    # Check PID liveness
    alive = pid is not None and proc_mgr.is_alive(pid)

    if not alive:
        # Process already dead -- stale run, mark as error
        db.update_run_status(latest.id, Status.ERROR, -1)
        return f"{name} was already dead \u2014 marked as error"

    # Process is alive -- ask the agent to stop it
    assert pid is not None  # alive==True implies pid is not None
    agent.stop(pid)

    # Update run status to stopped
    db.update_run_status(latest.id, Status.STOPPED, None)

    return f"{name} stopped"


# -- cmd_config --

def cmd_config(db: Database, name: str, config_pairs: List[str]) -> str:
    actor = db.get_actor(name)

    if not config_pairs:
        output = ""
        for key, value in sorted(actor.config.items()):
            output += f"{key}={value}\n"
        return output

    new_config = parse_config(config_pairs)

    merged = dict(actor.config)
    for key, value in new_config.items():
        merged[key] = value

    db.update_actor_config(name, _sorted_config(merged))

    return f"{name} config updated"


# -- cmd_logs --

def cmd_logs(db: Database, agent: Agent, name: str, verbose: bool, watch: bool) -> str:
    actor = db.get_actor(name)

    session_id = actor.agent_session
    if session_id is None:
        return "No session yet \u2014 run the actor first"

    entries = agent.read_logs(Path(actor.dir), session_id)

    if not entries:
        return "No log entries found."

    lines: List[str] = []
    for entry in entries:
        ts = _format_log_timestamp(entry.timestamp) if verbose else ""

        if entry.kind == LogEntryKind.USER:
            lines.append(f"{ts}USER: {entry.text}")
        elif entry.kind == LogEntryKind.ASSISTANT:
            lines.append(f"{ts}ASSISTANT: {entry.text}")
        elif entry.kind == LogEntryKind.THINKING and verbose:
            lines.append(f"{ts}THINKING: {entry.text}")
        elif entry.kind == LogEntryKind.TOOL_USE and verbose:
            lines.append(f"{ts}TOOL: {entry.name}({_truncate_input(entry.input, 80)})")
        elif entry.kind == LogEntryKind.TOOL_RESULT and verbose:
            lines.append(f"{ts}RESULT: {_truncate_input(entry.content, 120)}")

    return "\n".join(lines)


# -- cmd_done --

def cmd_done(
    db: Database,
    git: GitOps,
    proc_mgr: ProcessManager,
    name: str,
    merge: bool,
    pr: bool,
    discard: bool,
    title: Optional[str],
    body: Optional[str],
) -> str:
    actor = db.get_actor(name)

    # Check not running (with stale PID detection)
    status = db.resolve_actor_status(name, proc_mgr)
    if status == Status.RUNNING:
        raise IsRunningError(name)

    # Validate mutually exclusive flags
    flag_count = sum([merge, pr, discard])
    if flag_count > 1:
        raise ActorError("only one of --merge, --pr, or --discard can be specified")

    if actor.worktree:
        source_repo = actor.source_repo
        if source_repo is None:
            raise ActorError("worktree actor missing source_repo")
        repo_path = Path(source_repo)
        wt_path = Path(actor.dir)

        if merge:
            base = actor.base_branch
            if base is None:
                raise ActorError("no base branch set \u2014 cannot merge")
            git.merge_branch(repo_path, actor.name, base)
            git.remove_worktree(repo_path, wt_path)
            git.delete_branch(repo_path, actor.name)

            db.delete_actor(name)
            return f"{name} done (merged into {base})"

        elif pr:
            base = actor.base_branch
            if base is None:
                raise ActorError("no base branch set \u2014 cannot create PR")
            pr_title = title if title is not None else actor.name
            pr_body = body if body is not None else ""
            git.push_branch(repo_path, actor.name)
            url = git.create_pr(repo_path, actor.name, base, pr_title, pr_body)
            try:
                git.remove_worktree(repo_path, wt_path)
            except Exception as e:
                print(f"warning: failed to remove worktree at {wt_path}: {e}", file=sys.stderr)

            db.delete_actor(name)
            return f"{name} done (PR: {url})"

        elif discard:
            git.remove_worktree(repo_path, wt_path)
            git.delete_branch(repo_path, actor.name)

            db.delete_actor(name)
            return f"{name} done (discarded)"

        else:
            # Default: keep branch, remove worktree
            git.remove_worktree(repo_path, wt_path)

    # Delete actor and all runs
    db.delete_actor(name)

    if discard:
        return f"{name} done (discarded)"
    return f"{name} done"


# -- cmd_fork --

def cmd_fork(
    db: Database,
    git: GitOps,
    source_name: str,
    new_name: str,
) -> Actor:
    """Fork an existing actor into a new actor branched from its current state."""
    validate_name(new_name)

    source = db.get_actor(source_name)

    if not source.worktree:
        raise ActorError(f"cannot fork '{source_name}' — only worktree actors can be forked")

    source_dir = Path(source.dir)
    if not source_dir.is_dir():
        raise ActorError(f"actor directory '{source.dir}' does not exist")

    source_repo = source.source_repo
    if source_repo is None:
        raise ActorError(f"worktree actor '{source_name}' missing source_repo")

    # Commit any uncommitted changes in the source worktree
    import subprocess
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(source_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"actor fork: snapshot for {new_name}"],
            cwd=str(source_dir),
            capture_output=True,
        )

    # Create new worktree branched from the source actor's branch
    wt_path = _worktree_path(new_name)
    git.create_worktree(Path(source_repo), wt_path, new_name, source_name)

    now = _now_iso()
    actor = Actor(
        name=new_name,
        agent=source.agent,
        agent_session=None,
        dir=str(wt_path),
        source_repo=source_repo,
        base_branch=source.base_branch,
        worktree=True,
        config=dict(source.config),
        created_at=now,
        updated_at=now,
    )

    try:
        db.insert_actor(actor)
    except ActorError:
        try:
            git.remove_worktree(Path(source_repo), wt_path)
        except Exception as cleanup_err:
            print(f"warning: failed to clean up worktree at {wt_path}: {cleanup_err}", file=sys.stderr)
        raise

    return actor


# ---------------------------------------------------------------------------
# Public wrappers for testable internals
# ---------------------------------------------------------------------------

def truncate(s: str, max_len: int) -> str:
    """Public wrapper for _truncate."""
    return _truncate(s, max_len)


def format_duration(started_at: str, finished_at: Optional[str]) -> str:
    """Public wrapper for _format_duration."""
    return _format_duration(started_at, finished_at)


def worktree_path(name: str) -> Path:
    """Public wrapper for _worktree_path."""
    return _worktree_path(name)


def encode_dir(dir_path) -> str:
    """Public wrapper for ClaudeAgent._encode_dir."""
    return ClaudeAgent._encode_dir(Path(dir_path))


def claude_session_file_path(dir_path, session_id: str) -> str:
    """Public wrapper for ClaudeAgent._session_file_path. Returns string path."""
    return str(ClaudeAgent._session_file_path(Path(dir_path), session_id))


def claude_config_args(config: Config) -> List[str]:
    """Public wrapper for ClaudeAgent._config_args."""
    return ClaudeAgent._config_args(config)


def codex_config_args(config: Config) -> List[str]:
    """Public wrapper for CodexAgent._config_args."""
    return CodexAgent._config_args(config)


def claude_read_logs(path: str) -> List[LogEntry]:
    """Read Claude JSONL logs from a file path, returning LogEntry objects.

    This is a standalone function that wraps the JSONL parsing logic
    from ClaudeAgent.read_logs for direct file-path-based testing.
    """
    try:
        content = Path(path).read_text()
    except FileNotFoundError:
        return []

    entries: List[LogEntry] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = v.get("type")
        if not isinstance(msg_type, str):
            continue

        timestamp = v.get("timestamp")
        ts: Optional[str] = timestamp if isinstance(timestamp, str) else None

        message = v.get("message")
        if message is None:
            continue

        if msg_type == "user":
            content_val = message.get("content")
            if isinstance(content_val, str):
                entries.append(LogEntry(
                    kind=LogEntryKind.USER,
                    timestamp=ts,
                    text=content_val,
                ))
            elif isinstance(content_val, list):
                for item in content_val:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        c = item.get("content", "")
                        if isinstance(c, str):
                            text = c
                        else:
                            text = json.dumps(c) if c is not None else ""
                        entries.append(LogEntry(
                            kind=LogEntryKind.TOOL_RESULT,
                            timestamp=ts,
                            content=text,
                        ))
        elif msg_type == "assistant":
            content_arr = message.get("content")
            if isinstance(content_arr, list):
                for block in content_arr:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            entries.append(LogEntry(
                                kind=LogEntryKind.ASSISTANT,
                                timestamp=ts,
                                text=text,
                            ))
                    elif block_type == "thinking":
                        text = block.get("thinking")
                        if isinstance(text, str):
                            entries.append(LogEntry(
                                kind=LogEntryKind.THINKING,
                                timestamp=ts,
                                text=text,
                            ))
                    elif block_type == "tool_use":
                        name = block.get("name", "unknown")
                        inp = block.get("input")
                        inp_str = json.dumps(inp) if inp is not None else ""
                        entries.append(LogEntry(
                            kind=LogEntryKind.TOOL_USE,
                            timestamp=ts,
                            name=name,
                            input=inp_str,
                        ))

    return entries
