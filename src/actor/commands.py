from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from .errors import (
    ActorError,
    AgentNotFoundError,
    ConfigError,
    HookFailedError,
    IsRunningError,
    NotRunningError,
)

if TYPE_CHECKING:
    from .config import AppConfig, Hooks
from .hooks import HookRunner, hook_env, run_hook
from .interfaces import Agent, GitOps, LogEntry, LogEntryKind, ProcessManager, binary_exists
from .types import (
    Actor,
    ActorConfig,
    AgentKind,
    Run,
    Status,
    _now_iso,
    _parse_iso,
    _sorted_config,
    parse_config,
    validate_name,
)
from .db import Database
from .git import RealGit
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


_AGENT_CLASS_BY_KIND = {
    AgentKind.CLAUDE: ClaudeAgent,
    AgentKind.CODEX: CodexAgent,
}


def _agent_class(kind: AgentKind):
    try:
        return _AGENT_CLASS_BY_KIND[kind]
    except KeyError:
        raise ActorError(f"unknown agent kind: {kind}")


def _create_agent(kind: AgentKind) -> Agent:
    return _agent_class(kind)()



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
    agent_name: Optional[str],
    cli_overrides: ActorConfig,
    role_name: Optional[str] = None,
    app_config: Optional["AppConfig"] = None,
    hook_runner: Optional[HookRunner] = None,
) -> Actor:
    validate_name(name)

    role = None
    if role_name is not None:
        roles = app_config.roles if app_config is not None else {}
        if role_name not in roles:
            available = sorted(roles)
            hint = (
                f"available: {', '.join(available)}"
                if available
                else "no roles defined in settings.kdl"
            )
            raise ConfigError(f"unknown role: '{role_name}' ({hint})")
        role = roles[role_name]

    # Agent precedence: explicit CLI flag > role's agent > "claude"
    if agent_name is None:
        agent_name = role.agent if (role and role.agent) else "claude"
    agent_kind = AgentKind.from_str(agent_name)

    if not binary_exists(agent_kind.binary_name):
        print(f"warning: '{agent_kind.binary_name}' not found on PATH", file=sys.stderr)

    # Config precedence (lowest → highest), merged into two side-by-side
    # dicts (actor_keys, agent_args) — the split is preserved positionally
    # across every layer; nothing downstream reconstructs it via name lookup:
    #   1. Agent class defaults (ACTOR_DEFAULTS / AGENT_DEFAULTS baseline)
    #   2. kdl `defaults "<name>" { ... }` block for this agent_kind
    #   3. Role config (kdl role is a flat namespace; we partition
    #      each key here using the agent class's ACTOR_DEFAULTS whitelist)
    #   4. CLI overrides (already structured by the CLI layer, which also
    #      validates that `--config` keys don't collide with actor-keys)
    # Only layer 2 can carry `None` (kdl's `null` cancel marker). The other
    # layers are typed `Dict[str, str]` and contribute only concrete values.
    agent_cls = _agent_class(agent_kind)
    merged_actor_keys: Dict[str, Optional[str]] = dict(agent_cls.ACTOR_DEFAULTS)
    merged_agent_args: Dict[str, Optional[str]] = dict(agent_cls.AGENT_DEFAULTS)

    # Layer 2: kdl agent_defaults for this agent_kind (if any). `None` values
    # cancel lower-precedence entries by popping them from the bucket.
    if app_config is not None:
        kdl_defaults = app_config.agent_defaults.get(agent_kind.value)
        if kdl_defaults is not None:
            for k, v in kdl_defaults.actor_keys.items():
                if v is None:
                    merged_actor_keys.pop(k, None)
                else:
                    merged_actor_keys[k] = v
            for k, v in kdl_defaults.agent_args.items():
                if v is None:
                    merged_agent_args.pop(k, None)
                else:
                    merged_agent_args[k] = v

    # Layer 3: role config. The kdl role namespace is flat, so we
    # partition by checking each key against the agent's ACTOR_DEFAULTS
    # whitelist — this is an input-boundary split, not runtime routing.
    if role is not None:
        for k, v in role.config.items():
            if k in agent_cls.ACTOR_DEFAULTS:
                merged_actor_keys[k] = v
            else:
                merged_agent_args[k] = v

    # Layer 4: CLI overrides (already split by the CLI layer).
    for k, v in cli_overrides.actor_keys.items():
        merged_actor_keys[k] = v
    for k, v in cli_overrides.agent_args.items():
        merged_agent_args[k] = v

    config = ActorConfig(
        actor_keys=_sorted_config({k: v for k, v in merged_actor_keys.items() if v is not None}),
        agent_args=_sorted_config({k: v for k, v in merged_agent_args.items() if v is not None}),
    )

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

    parent = os.environ.get("ACTOR_NAME")

    now = _now_iso()
    actor = Actor(
        name=name,
        agent=agent_kind,
        agent_session=None,
        dir=actor_dir,
        source_repo=source_repo,
        base_branch=base_branch,
        worktree=worktree,
        parent=parent,
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

    # on-start hook fires after the actor row + worktree exist so the hook
    # script can assume both. Non-zero exit rolls everything back.
    on_start = app_config.hooks.on_start if app_config is not None else None
    if on_start is not None:
        env = hook_env(
            os.environ,
            actor_name=name,
            actor_dir=Path(actor_dir),
            actor_agent=agent_kind.value,
            actor_session_id=None,
        )
        try:
            run_hook("on-start", on_start, env, Path(actor_dir), runner=hook_runner)
        except Exception:
            try:
                db.delete_actor(name)
            except Exception as rollback_err:
                print(
                    f"warning: failed to roll back actor row for '{name}' "
                    f"after on-start hook failure: {rollback_err}",
                    file=sys.stderr,
                )
            if worktree:
                assert source_repo is not None
                wt_path = _worktree_path(name)
                try:
                    git.remove_worktree(Path(source_repo), wt_path)
                except Exception as cleanup_err:
                    print(
                        f"warning: failed to clean up worktree at {wt_path}: {cleanup_err}",
                        file=sys.stderr,
                    )
            raise

    return actor


# -- cmd_run --

def cmd_run(
    db: Database,
    agent: Agent,
    proc_mgr: ProcessManager,
    name: str,
    prompt: str,
    cli_overrides: ActorConfig,
    app_config: Optional["AppConfig"] = None,
    hook_runner: Optional[HookRunner] = None,
) -> str:
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
            f"actor directory '{actor.dir}' does not exist \u2014 use 'actor discard {name}' to clean up"
        )

    # before-run hook fires before the Run row is inserted so a failing
    # pre-flight check doesn't leave a phantom run behind.
    before_run = app_config.hooks.before_run if app_config is not None else None
    if before_run is not None:
        env = hook_env(
            os.environ,
            actor_name=name,
            actor_dir=dir_path,
            actor_agent=actor.agent.value,
            actor_session_id=actor.agent_session,
        )
        run_hook("before-run", before_run, env, dir_path, runner=hook_runner)

    # Merge config: actor defaults + run overrides. The split is preserved
    # — actor_keys and agent_args are layered independently. cli_overrides
    # is already structured by the CLI (--config pairs are agent_args only,
    # validated at the CLI boundary).
    merged_actor_keys = dict(actor.config.actor_keys)
    merged_actor_keys.update(cli_overrides.actor_keys)
    merged_agent_args = dict(actor.config.agent_args)
    merged_agent_args.update(cli_overrides.agent_args)
    effective_config = ActorConfig(
        actor_keys=_sorted_config(merged_actor_keys),
        agent_args=_sorted_config(merged_agent_args),
    )

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
    db.touch_actor(name)

    # Expose actor name to the agent process (set for child, cleaned up after)
    prev_actor_name = os.environ.get("ACTOR_NAME")
    os.environ["ACTOR_NAME"] = name

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
    finally:
        # Restore ACTOR_NAME so the MCP server process isn't polluted
        if prev_actor_name is None:
            os.environ.pop("ACTOR_NAME", None)
        else:
            os.environ["ACTOR_NAME"] = prev_actor_name

    # Update PID on the run row
    db.update_run_pid(run_id, pid)

    # Update session ID if we got one
    if new_session is not None:
        db.update_actor_session(name, new_session)

    # Block until agent exits
    exit_code, output = agent.wait(pid)

    # Check if stop command already updated this run (race condition)
    current_run = db.latest_run(name)
    if current_run is not None and current_run.id == run_id and current_run.status == Status.STOPPED:
        return output

    run_status = Status.DONE if exit_code == 0 else Status.ERROR
    db.update_run_status(run_id, run_status, exit_code)

    # after-run hook fires AFTER the DB has been updated with the final
    # status so a hook that runs `actor show` sees the completed run.
    # Non-zero exit is logged to stderr but does NOT fail the run — the
    # agent has already finished and there's nothing to roll back.
    after_run = app_config.hooks.after_run if app_config is not None else None
    if after_run is not None:
        # Refetch so ACTOR_SESSION_ID reflects any new_session set above.
        refreshed = db.get_actor(name)
        start = _parse_iso(run.started_at)
        end = _parse_iso(_now_iso())
        duration_ms = None
        if start is not None and end is not None:
            duration_ms = max(0, int((end - start).total_seconds() * 1000))
        env = hook_env(
            os.environ,
            actor_name=name,
            actor_dir=dir_path,
            actor_agent=refreshed.agent.value,
            actor_session_id=refreshed.agent_session,
            actor_run_id=run_id,
            actor_exit_code=exit_code,
            actor_duration_ms=duration_ms,
        )
        try:
            run_hook("after-run", after_run, env, dir_path, runner=hook_runner)
        except HookFailedError as e:
            print(f"warning: {e}", file=sys.stderr)
    return output


# -- cmd_interactive --

INTERACTIVE_PROMPT = "*interactive*"


def cmd_interactive(
    db: Database,
    agent: Agent,
    proc_mgr: ProcessManager,
    name: str,
    runner: Optional[Callable[[List[str], Path, dict], int]] = None,
    app_config: Optional["AppConfig"] = None,
    hook_runner: Optional[HookRunner] = None,
) -> Tuple[int, str]:
    """`runner` is injectable so tests can drive without spawning a real
    subprocess. Returns (exit_code, status_message)."""
    actor = db.get_actor(name)

    status = db.resolve_actor_status(name, proc_mgr)
    if status == Status.RUNNING:
        raise IsRunningError(name)

    session_id = actor.agent_session
    if session_id is None:
        raise ActorError(
            f"'{name}' has no session yet \u2014 run it non-interactively first"
        )

    if not binary_exists(actor.agent.binary_name):
        raise AgentNotFoundError(actor.agent.binary_name)

    dir_path = Path(actor.dir)
    if not dir_path.is_dir():
        raise ActorError(
            f"actor directory '{actor.dir}' does not exist \u2014 use 'actor discard {name}' to clean up"
        )

    # before-run hook mirrors cmd_run. Fires before the Run row is inserted.
    before_run = app_config.hooks.before_run if app_config is not None else None
    if before_run is not None:
        env = hook_env(
            os.environ,
            actor_name=name,
            actor_dir=dir_path,
            actor_agent=actor.agent.value,
            actor_session_id=actor.agent_session,
        )
        run_hook("before-run", before_run, env, dir_path, runner=hook_runner)

    argv = agent.interactive_argv(session_id, actor.config)

    run = Run(
        id=0,
        actor_name=name,
        prompt=INTERACTIVE_PROMPT,
        status=Status.RUNNING,
        exit_code=None,
        pid=None,
        config=actor.config,
        started_at=_now_iso(),
        finished_at=None,
    )
    run_id = db.insert_run(run)
    db.touch_actor(name)

    env = dict(os.environ)
    env["ACTOR_NAME"] = name

    try:
        exit_code = (runner or _default_interactive_runner)(argv, dir_path, env)
    except BaseException:
        db.update_run_status(run_id, Status.ERROR, -1)
        raise

    current = db.latest_run(name)
    if current is not None and current.id == run_id and current.status == Status.STOPPED:
        return exit_code, f"Interactive session for '{name}' stopped."

    final = Status.DONE if exit_code == 0 else Status.ERROR
    db.update_run_status(run_id, final, exit_code)

    # after-run hook (same semantics as cmd_run): fires AFTER the DB
    # update with final status. Non-zero exit logs a warning but doesn't
    # fail the completed session.
    after_run = app_config.hooks.after_run if app_config is not None else None
    if after_run is not None:
        refreshed = db.get_actor(name)
        start = _parse_iso(run.started_at)
        end_ts = _parse_iso(_now_iso())
        duration_ms = None
        if start is not None and end_ts is not None:
            duration_ms = max(0, int((end_ts - start).total_seconds() * 1000))
        after_env = hook_env(
            os.environ,
            actor_name=name,
            actor_dir=dir_path,
            actor_agent=refreshed.agent.value,
            actor_session_id=refreshed.agent_session,
            actor_run_id=run_id,
            actor_exit_code=exit_code,
            actor_duration_ms=duration_ms,
        )
        try:
            run_hook("after-run", after_run, after_env, dir_path, runner=hook_runner)
        except HookFailedError as e:
            print(f"warning: {e}", file=sys.stderr)
    return exit_code, f"Interactive session for '{name}' ended (exit {exit_code})."


def _default_interactive_runner(argv: List[str], cwd: Path, env: dict) -> int:
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                return proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Give up rather than block the shell forever. Surface a
                # warning so the user can chase a zombie/stuck process.
                print(
                    f"warning: child pid {proc.pid} did not exit after SIGKILL",
                    file=sys.stderr,
                )
                return -1


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

def cmd_roles(app_config: "AppConfig") -> str:
    roles = app_config.roles
    if not roles:
        return "No roles defined. Add a `role \"<name>\" { ... }` block to ~/.actor/settings.kdl or <repo>/.actor/settings.kdl.\n"

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

    # Display-only: merge both buckets for the user-facing `Config:` line.
    # Both dicts are disjoint by construction (an ACTOR_DEFAULTS key only
    # lands in actor_keys; everything else lands in agent_args), so the
    # merge is lossless.
    if actor.config.actor_keys or actor.config.agent_args:
        flat = dict(actor.config.agent_args)
        flat.update(actor.config.actor_keys)
        pairs = [f"{k}={v}" for k, v in sorted(flat.items())]
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
        # Display merged view — both dicts are disjoint by construction so
        # the flatten for display is lossless.
        flat = dict(actor.config.agent_args)
        flat.update(actor.config.actor_keys)
        output = ""
        for key, value in sorted(flat.items()):
            output += f"{key}={value}\n"
        return output

    # Partition new pairs into actor_keys / agent_args using the actor's
    # agent class whitelist. This is the single boundary where user-entered
    # flat pairs get lifted into the split structure; the merge itself and
    # all downstream layers operate positionally.
    updates = parse_config(config_pairs)
    agent_cls = _agent_class(actor.agent)
    new_actor_keys = dict(actor.config.actor_keys)
    new_agent_args = dict(actor.config.agent_args)
    for k, v in updates.items():
        if k in agent_cls.ACTOR_DEFAULTS:
            new_actor_keys[k] = v
        else:
            new_agent_args[k] = v
    new_config = ActorConfig(
        actor_keys=_sorted_config(new_actor_keys),
        agent_args=_sorted_config(new_agent_args),
    )

    db.update_actor_config(name, new_config)

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


# -- cmd_discard --

def _force_stop(db: Database, proc_mgr: ProcessManager, name: str) -> None:
    """Stop a running actor by killing its process."""
    latest = db.latest_run(name)
    if latest is None or latest.status != Status.RUNNING:
        return
    pid = latest.pid
    if pid is not None and proc_mgr.is_alive(pid):
        proc_mgr.kill(pid)
    db.update_run_status(latest.id, Status.STOPPED, None)


_DEFAULT_ON_DISCARD = "git diff --quiet"
"""Default on-discard hook command. Fires when no `on-discard` is
configured in the user's / project's settings.kdl. The intent is
"don't let the user accidentally throw away uncommitted work" —
`git diff --quiet` exits 0 only when the worktree is clean, so
`actor discard` aborts if there are pending changes (unless
`--force` is set). To suppress entirely without configuring a real
check, set `on-discard "true"` in settings.kdl."""


def cmd_discard(
    db: Database,
    proc_mgr: ProcessManager,
    name: str,
    *,
    git: Optional[GitOps] = None,
    _visited: set[str] | None = None,
    app_config: Optional["AppConfig"] = None,
    hook_runner: Optional[HookRunner] = None,
    force: bool = False,
) -> str:
    """Discard an actor: stop it if running, run the on-discard hook,
    remove the worktree, delete the DB row.

    Order matters. Children (recursively discovered via the `parent`
    column) are processed depth-first — leaves first — so a parent
    is only deleted once all its descendants are gone. If any
    discard in the chain raises (failing on-discard, worktree
    removal error, etc.) the chain stops and the surfaced exception
    names the actor that broke the chain.

    Default `on-discard` is `git diff --quiet` so a worktree with
    pending edits won't be wiped accidentally; users can set
    `on-discard "..."` (or `on-discard "true"` to suppress) in
    settings.kdl. `--force` (or `force=True`) bypasses the hook
    failure and proceeds anyway."""
    if git is None:
        git = RealGit()

    actor = db.get_actor(name)

    # Track visited to prevent infinite recursion on circular parent
    # chains (shouldn't happen in practice but defensive).
    if _visited is None:
        _visited = set()
    _visited.add(name)

    # Recursively discard children FIRST (leaves up). If any child's
    # discard raises and `force` is False, that exception propagates
    # — the chain stops and we never touch this actor's worktree or
    # DB row. Matches the user expectation of "if one on-discard
    # fails, stop".
    children = db.list_children(name)
    discarded = []
    for child in children:
        if child.name not in _visited:
            msg = cmd_discard(
                db, proc_mgr, name=child.name, git=git, _visited=_visited,
                app_config=app_config, hook_runner=hook_runner, force=force,
            )
            discarded.append(msg)

    # Stop if running (SIGTERM/SIGKILL the agent process). Has to
    # happen BEFORE the hook so the hook runs against settled
    # working-tree state.
    status = db.resolve_actor_status(name, proc_mgr)
    if status == Status.RUNNING:
        _force_stop(db, proc_mgr, name)

    # Resolve on-discard hook command. Default to a safety check
    # ONLY when an `app_config` was passed — tests that bypass
    # config layering shouldn't get bitten by a `git diff` they
    # didn't ask for.
    if app_config is not None:
        on_discard = app_config.hooks.on_discard or _DEFAULT_ON_DISCARD
    else:
        on_discard = None

    if on_discard is not None:
        actor_dir = Path(actor.dir)
        hook_cwd = actor_dir if actor_dir.is_dir() else Path.home()
        env = hook_env(
            os.environ,
            actor_name=name,
            actor_dir=actor_dir,
            actor_agent=actor.agent.value,
            actor_session_id=actor.agent_session,
        )
        try:
            run_hook("on-discard", on_discard, env, hook_cwd, runner=hook_runner)
        except HookFailedError as e:
            if force:
                print(
                    f"warning: on-discard hook failed for '{name}' but "
                    f"--force was set; discarding anyway: {e}",
                    file=sys.stderr,
                )
            else:
                # Wrap with actor context so the error message tells
                # the agent / user exactly which actor's hook tripped
                # — important for chained discards where the failing
                # one might not be the actor the user typed.
                raise HookFailedError(
                    e.event,
                    e.command,
                    e.exit_code,
                    stdout=e.stdout,
                    stderr=(
                        f"actor '{name}' discard aborted; "
                        f"{e.stderr}" if e.stderr
                        else f"actor '{name}' discard aborted"
                    ),
                ) from e

    # Remove the worktree if the actor was created with one. Failure
    # here also aborts the discard (no DB delete) unless force —
    # otherwise the user would end up with a dangling worktree on
    # disk and no DB row referring to it.
    if actor.worktree and actor.source_repo:
        wt_path = Path(actor.dir)
        if wt_path.is_dir():
            try:
                git.remove_worktree(Path(actor.source_repo), wt_path)
            except Exception as e:
                if force:
                    print(
                        f"warning: failed to remove worktree {wt_path} for "
                        f"'{name}'; --force is set so DB delete proceeds: {e}",
                        file=sys.stderr,
                    )
                else:
                    raise ActorError(
                        f"failed to remove worktree {wt_path} for actor "
                        f"'{name}': {e}"
                    ) from e

    db.delete_actor(name)
    discarded.append(f"{name} discarded")
    return "\n".join(discarded)



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


def claude_read_logs(path: str) -> List[LogEntry]:
    """Read Claude JSONL logs from a file path, returning LogEntry objects.

    Thin wrapper around ``ClaudeAgent._parse_entries`` for direct
    file-path-based testing — exists so tests can parse without
    constructing a ClaudeAgent or a session directory structure."""
    try:
        content = Path(path).read_text()
    except FileNotFoundError:
        return []
    return ClaudeAgent._parse_entries(content)
