"""Display formatters for CLI / MCP output.

The service layer returns structured types (`ActorDetail`,
`DiscardResult`, …); this module renders them as the user-facing
strings that `actor list`, `actor show`, etc. emit. Service callers
that don't need the human-readable form (programmatic consumers,
tests, future RemoteActorService) can ignore this module entirely.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .agents.claude import ClaudeAgent
from .interfaces import LogEntry, LogEntryKind
from .service import ActorDetail, DiscardResult, LogsResult, StopResult
from .config import Role
from .types import Actor, Run, Status, _parse_iso


# ---------------------------------------------------------------------------
# Generic helpers (also re-exported via the actor package for tests)
# ---------------------------------------------------------------------------


def truncate(s: str, max_len: int) -> str:
    """Truncate to first line and max chars, appending '...' if longer."""
    first_line = s.split("\n", 1)[0]
    chars = list(first_line)
    if len(chars) <= max_len:
        return first_line
    return "".join(chars[:max_len]) + "..."


def format_duration(started_at: str, finished_at: Optional[str]) -> str:
    """Format a duration between two ISO timestamps as 'Xm Ys'."""
    if finished_at is None:
        return "—"
    start = _parse_iso(started_at)
    end = _parse_iso(finished_at)
    if start is None or end is None:
        return "—"
    secs = max(0, int((end - start).total_seconds()))
    minutes = secs // 60
    remainder = secs % 60
    if minutes > 0:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def worktree_path(name: str) -> Path:
    """Public wrapper for the standard worktree path layout used by tests."""
    from .service import _worktree_path
    return _worktree_path(name)


def encode_dir(dir_path) -> str:
    """Public wrapper for ClaudeAgent._encode_dir."""
    return ClaudeAgent._encode_dir(Path(dir_path))


def claude_session_file_path(dir_path, session_id: str) -> str:
    """Public wrapper for ClaudeAgent._session_file_path. Returns string path."""
    return str(ClaudeAgent._session_file_path(Path(dir_path), session_id))


def claude_read_logs(path: str) -> List[LogEntry]:
    """Read Claude JSONL logs from a file path.

    Thin wrapper around ``ClaudeAgent._parse_entries`` for direct
    file-path-based testing — exists so tests can parse without
    constructing a ClaudeAgent or a session directory structure."""
    try:
        content = Path(path).read_text()
    except FileNotFoundError:
        return []
    return ClaudeAgent._parse_entries(content)


# ---------------------------------------------------------------------------
# Helpers private to this module
# ---------------------------------------------------------------------------


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
# Service-result formatters
# ---------------------------------------------------------------------------


def format_actor_table(
    actors: List[Actor],
    statuses: Dict[str, Status],
    latest_runs: Dict[str, Optional[Run]],
) -> str:
    """Render the `actor list` table.

    `statuses` and `latest_runs` are keyed by actor name. Pre-computed
    by the caller so the formatter stays pure (no DB / proc-mgr
    access).
    """
    rows: List[Tuple[str, Status, str]] = []
    for actor in actors:
        st = statuses.get(actor.name, Status.IDLE)
        latest = latest_runs.get(actor.name)
        prompt = truncate(latest.prompt, 40) if latest is not None else ""
        rows.append((actor.name, st, prompt))

    h_name = "NAME"
    h_status = "STATUS"
    h_prompt = "PROMPT"

    name_width = max((len(r[0]) for r in rows), default=0)
    name_width = max(name_width, len(h_name))
    status_width = max((len(r[1].as_str()) for r in rows), default=0)
    status_width = max(status_width, len(h_status))

    output = f"{h_name:<{name_width}}  {h_status:<{status_width}}  {h_prompt}\n"
    for name, st, prompt in rows:
        output += (
            f"{name:<{name_width}}  {st.as_str():<{status_width}}  {prompt}\n"
        )
    return output


def format_actor_detail(detail: ActorDetail) -> str:
    """Render the `actor show` page — actor metadata + run history."""
    actor = detail.actor
    output = ""
    output += f"Name:      {actor.name}\n"
    output += f"Agent:     {actor.agent}\n"
    output += f"Status:    {detail.status}\n"
    output += f"Dir:       {actor.dir}\n"

    if actor.base_branch is not None:
        output += f"Base:      {actor.base_branch}\n"

    if actor.parent is not None:
        output += f"Parent:    {actor.parent}\n"

    # Display-only: merge both buckets for the user-facing `Config:`
    # line. Both dicts are disjoint by construction so the merge is
    # lossless.
    if actor.config.actor_keys or actor.config.agent_args:
        flat = dict(actor.config.agent_args)
        flat.update(actor.config.actor_keys)
        pairs = [f"{k}={v}" for k, v in sorted(flat.items())]
        output += f"Config:    {', '.join(pairs)}\n"

    if actor.agent_session is not None:
        output += f"Session:   {actor.agent_session}\n"

    output += f"Created:   {actor.created_at}\n"

    if detail.runs_limit == 0:
        # Caller asked us to hide the runs section entirely.
        return output

    output += "\n"
    if not detail.runs:
        output += "No runs yet.\n"
        return output

    output += format_run_history(detail.runs, detail.total_runs)
    return output


def format_run_history(runs: List[Run], total: int) -> str:
    """Render the runs sub-table for `actor show`."""
    run_rows: List[Tuple[str, str, str, str, str]] = []
    for r in runs:
        run_rows.append((
            str(r.id),
            r.status.as_str(),
            truncate(r.prompt, 40),
            format_duration(r.started_at, r.finished_at),
            str(r.exit_code) if r.exit_code is not None else "—",
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

    output = (
        f"{h_run:<{w_run}}  {h_status:<{w_status}}  "
        f"{h_prompt:<{w_prompt}}  {h_duration:<{w_duration}}  {h_exit}\n"
    )
    for rid, st, pr, dur, ex in run_rows:
        output += (
            f"{rid:<{w_run}}  {st:<{w_status}}  "
            f"{pr:<{w_prompt}}  {dur:<{w_duration}}  {ex}\n"
        )
    if total > len(runs):
        output += f"\n{total} total runs — use --runs to show more\n"
    return output


def format_logs(logs: LogsResult, verbose: bool = False) -> str:
    """Render the `actor logs` output."""
    if logs.session_id is None:
        return "No session yet — run the actor first"
    if not logs.entries:
        return "No log entries found."

    lines: List[str] = []
    for entry in logs.entries:
        ts = _format_log_timestamp(entry.timestamp) if verbose else ""
        if entry.kind == LogEntryKind.USER:
            lines.append(f"{ts}USER: {entry.text}")
        elif entry.kind == LogEntryKind.ASSISTANT:
            lines.append(f"{ts}ASSISTANT: {entry.text}")
        elif entry.kind == LogEntryKind.THINKING and verbose:
            lines.append(f"{ts}THINKING: {entry.text}")
        elif entry.kind == LogEntryKind.TOOL_USE and verbose:
            lines.append(
                f"{ts}TOOL: {entry.name}({_truncate_input(entry.input, 80)})"
            )
        elif entry.kind == LogEntryKind.TOOL_RESULT and verbose:
            lines.append(f"{ts}RESULT: {_truncate_input(entry.content, 120)}")
    return "\n".join(lines)


def format_roles(roles: Dict[str, Role]) -> str:
    """Render the `actor roles` listing."""
    if not roles:
        return (
            "No roles defined. Add a `role \"<name>\" { ... }` block to "
            "~/.actor/settings.kdl or <repo>/.actor/settings.kdl.\n"
        )

    h_name = "NAME"
    h_agent = "AGENT"
    h_desc = "DESCRIPTION"

    rows: List[Tuple[str, str, str]] = []
    for name in sorted(roles):
        r = roles[name]
        agent = r.agent or "claude"
        desc = r.description or ""
        rows.append((name, agent, desc))

    name_width = max((len(r[0]) for r in rows), default=0)
    name_width = max(name_width, len(h_name))
    agent_width = max((len(r[1]) for r in rows), default=0)
    agent_width = max(agent_width, len(h_agent))

    output = f"{h_name:<{name_width}}  {h_agent:<{agent_width}}  {h_desc}\n"
    for name, agent, desc in rows:
        output += f"{name:<{name_width}}  {agent:<{agent_width}}  {desc}\n"
    return output


def format_discard(result: DiscardResult) -> str:
    """Render the message returned by `actor discard`."""
    return "\n".join(f"{n} discarded" for n in result.names)


def format_stop(result: StopResult) -> str:
    """Render the message returned by `actor stop`."""
    if result.was_alive:
        return f"{result.name} stopped"
    return f"{result.name} was already dead — marked as error"


def format_config_view(config) -> str:
    """Render the `actor config <name>` (no pairs) view."""
    flat = dict(config.agent_args)
    flat.update(config.actor_keys)
    output = ""
    for key, value in sorted(flat.items()):
        output += f"{key}={value}\n"
    return output
