from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .errors import ActorError
from .interfaces import Agent
from .types import AgentKind
from .db import Database
from .git import RealGit
from .process import RealProcessManager
from .agents.claude import ClaudeAgent
from .agents.codex import CodexAgent
from .commands import (
    cmd_config,
    cmd_done,
    cmd_list,
    cmd_logs,
    cmd_new,
    cmd_run,
    cmd_show,
    cmd_stop,
)


def _create_agent(kind: AgentKind) -> Agent:
    if kind == AgentKind.CLAUDE:
        return ClaudeAgent()
    elif kind == AgentKind.CODEX:
        return CodexAgent()
    else:
        raise ActorError(f"unknown agent kind: {kind}")


def _db_path() -> str:
    home = os.environ.get("HOME", "")
    if not home:
        raise ActorError("HOME environment variable is not set")
    return f"{home}/.actor/actor.db"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="actor",
        description="Orchestrate coding agents in parallel",
    )
    sub = parser.add_subparsers(dest="command")

    # -- new --
    p_new = sub.add_parser(
        "new",
        help="Create a new actor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor new my-feature                              Worktree from current repo
  actor new my-feature --no-worktree                Use current directory directly
  actor new my-feature --dir /path/to/repo          Worktree from another repo
  actor new my-feature --base develop               Branch off develop
  actor new my-feature --config model=sonnet        Set agent config at creation""",
    )
    p_new.add_argument("name", help="Actor name")
    p_new.add_argument("--dir", default=None, help="Base directory (defaults to CWD)")
    p_new.add_argument("--no-worktree", action="store_true", help="Skip worktree creation, run in the directory directly")
    p_new.add_argument("--base", default=None, help="Branch to create the worktree from (defaults to current branch)")
    p_new.add_argument("--agent", default="claude", help="Coding agent to use")
    p_new.add_argument("--config", dest="config", nargs="+", default=[], metavar="KEY=VALUE", help="Config key=value pairs")

    # -- run --
    p_run = sub.add_parser(
        "run",
        help="Execute a prompt against an actor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor run my-feature "fix the nav bar"            Run a prompt
  actor run my-feature "fix it" --config model=opus  Override config for this run
  actor run my-feature -i                            Resume interactively""",
    )
    p_run.add_argument("name", help="Actor name")
    p_run.add_argument("prompt", nargs="?", default=None, help="Prompt to send to the agent")
    p_run.add_argument("-i", "--interactive", action="store_true", help="Resume the actor in interactive mode")
    p_run.add_argument("--config", dest="config", nargs="+", default=[], metavar="KEY=VALUE", help="Config overrides for this run")

    # -- list --
    p_list = sub.add_parser(
        "list",
        help="List all actors",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor list                                        Show all actors
  actor list --status running                       Show only running actors""",
    )
    p_list.add_argument("--status", default=None, help="Filter by status")

    # -- show --
    p_show = sub.add_parser(
        "show",
        help="Show full details for an actor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor show my-feature                             Details + last 5 runs
  actor show my-feature --runs 20                   Show more run history
  actor show my-feature --runs 0                    Details only, no runs""",
    )
    p_show.add_argument("name", help="Actor name")
    p_show.add_argument("--runs", type=int, default=5, help="Number of recent runs to display")

    # -- logs --
    p_logs = sub.add_parser(
        "logs",
        help="View agent session output",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor logs my-feature                             Show prompts and responses
  actor logs my-feature --verbose                   Include tool calls, thinking, timestamps
  actor logs my-feature --watch                     Stream output live""",
    )
    p_logs.add_argument("name", help="Actor name")
    p_logs.add_argument("--verbose", "-v", action="store_true", help="Show tool calls, thinking, and timestamps")
    p_logs.add_argument("--watch", action="store_true", help="Stream output as it happens")

    # -- stop --
    p_stop = sub.add_parser(
        "stop",
        help="Kill a running actor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor stop my-feature                             Stop the running agent""",
    )
    p_stop.add_argument("name", help="Actor name")

    # -- config --
    p_config = sub.add_parser(
        "config",
        help="View or update actor config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor config my-feature                           View current config
  actor config my-feature model=opus                Set config values
  actor config my-feature model=sonnet max-budget-usd=5""",
    )
    p_config.add_argument("name", help="Actor name")
    p_config.add_argument("pairs", nargs="*", default=[], metavar="KEY=VALUE", help="Config key=value pairs to set (omit to view)")

    # -- done --
    p_done = sub.add_parser(
        "done",
        help="Remove an actor from the database (worktree stays on disk)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor done my-feature                             Remove actor from DB""",
    )
    p_done.add_argument("name", help="Actor name")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        db = Database.open(_db_path())
    except Exception as e:
        print(f"error: failed to open database: {e}", file=sys.stderr)
        sys.exit(1)

    git = RealGit()
    proc_mgr = RealProcessManager()

    def agent_for(name: str) -> Agent:
        actor = db.get_actor(name)
        return _create_agent(actor.agent)

    try:
        if args.command == "new":
            actor = cmd_new(
                db, git,
                name=args.name,
                dir=args.dir,
                no_worktree=args.no_worktree,
                base=args.base,
                agent_name=args.agent,
                config_pairs=args.config,
            )
            print(f"{actor.name} created ({actor.dir})")

        elif args.command == "run":
            if args.interactive:
                actor = db.get_actor(args.name)
                dir_path = actor.dir
                session_id = actor.agent_session
                if session_id is None:
                    raise ActorError(f"'{args.name}' has no session yet — run it non-interactively first")
                os.chdir(dir_path)
                if actor.agent == AgentKind.CLAUDE:
                    cmd = ["claude", "--resume", session_id]
                elif actor.agent == AgentKind.CODEX:
                    cmd = ["codex", "resume", session_id]
                else:
                    raise ActorError(f"interactive mode not supported for agent: {actor.agent}")
                os.execvp(cmd[0], cmd)
            else:
                if args.prompt is None:
                    print("error: prompt is required (or use -i for interactive mode)", file=sys.stderr)
                    sys.exit(1)
                agent = agent_for(args.name)
                cmd_run(
                    db, agent, proc_mgr,
                    name=args.name,
                    prompt=args.prompt,
                    config_pairs=args.config,
                )

        elif args.command == "list":
            output = cmd_list(db, proc_mgr, status_filter=args.status)
            print(output, end="")

        elif args.command == "show":
            output = cmd_show(db, proc_mgr, name=args.name, runs_limit=args.runs)
            print(output, end="")

        elif args.command == "logs":
            agent = agent_for(args.name)
            output = cmd_logs(
                db, agent,
                name=args.name,
                verbose=args.verbose,
                watch=args.watch,
            )
            if output:
                print(output)

        elif args.command == "stop":
            agent = agent_for(args.name)
            msg = cmd_stop(db, agent, proc_mgr, name=args.name)
            print(msg)

        elif args.command == "config":
            output = cmd_config(db, name=args.name, config_pairs=args.pairs)
            if output:
                print(output, end="")

        elif args.command == "done":
            msg = cmd_done(db, proc_mgr, name=args.name)
            print(msg)

    except ActorError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
