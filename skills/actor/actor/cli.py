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
  actor new my-feature --model sonnet               Use a specific model
  actor new my-feature --no-strip-api-keys          Pass API keys to the agent
  actor new my-feature --no-worktree                Use current directory directly
  actor new my-feature --dir /path/to/repo          Worktree from another repo
  actor new my-feature --base develop               Branch off develop
  actor new my-feature --config effort=max          Set agent config at creation""",
    )
    p_new.add_argument("name", help="Actor name")
    p_new.add_argument("--dir", default=None, help="Base directory (defaults to CWD)")
    p_new.add_argument("--no-worktree", action="store_true", help="Skip worktree creation, run in the directory directly")
    p_new.add_argument("--base", default=None, help="Branch to create the worktree from (defaults to current branch)")
    p_new.add_argument("--agent", default="claude", help="Coding agent to use")
    p_new.add_argument("--model", default=None, help="Model for the agent to use")
    p_new.add_argument("--strip-api-keys", action="store_true", default=True, dest="strip_api_keys", help="Strip API keys from environment (default)")
    p_new.add_argument("--no-strip-api-keys", action="store_false", dest="strip_api_keys", help="Pass API keys through to the agent")
    p_new.add_argument("--config", dest="config", nargs="+", default=[], metavar="KEY=VALUE", help="Config key=value pairs")

    # -- run --
    p_run = sub.add_parser(
        "run",
        help="Create and/or run an actor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor run fix-nav -c "fix the nav bar"             Create actor and run
  actor run fix-nav "continue fixing"                Resume existing actor
  actor run fix-nav -c --model opus "fix it"         Create with specific model
  actor run fix-nav -c --agent codex "fix it"        Create with Codex agent
  actor run fix-nav --model opus "one-off override"  Override model for this run
  actor run fix-nav -i                               Resume interactively
  echo "fix it" | actor run fix-nav -c               Prompt from stdin""",
    )
    p_run.add_argument("name", help="Actor name")
    p_run.add_argument("prompt", nargs="?", default=None, help="Prompt (reads stdin if omitted and not interactive)")
    p_run.add_argument("-c", "--create", action="store_true", help="Create the actor first (worktree from current repo)")
    p_run.add_argument("-i", "--interactive", action="store_true", help="Resume the actor in interactive mode")
    # Shared flags (used for both creation and run overrides)
    p_run.add_argument("--model", default=None, help="Model for the agent to use")
    p_run.add_argument("--strip-api-keys", action="store_true", default=None, dest="strip_api_keys", help="Strip API keys from environment (default)")
    p_run.add_argument("--no-strip-api-keys", action="store_false", dest="strip_api_keys", help="Pass API keys through to the agent")
    p_run.add_argument("--config", dest="config", nargs="+", default=[], metavar="KEY=VALUE", help="Config key=value pairs")
    # Creation-only flags (require -c)
    p_run.add_argument("--agent", default="claude", help="Coding agent to use (requires -c)")
    p_run.add_argument("--dir", default=None, help="Base directory (requires -c)")
    p_run.add_argument("--base", default=None, help="Branch to create worktree from (requires -c)")
    p_run.add_argument("--no-worktree", action="store_true", help="Skip worktree creation (requires -c)")

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
            config_pairs = list(args.config)
            if args.model is not None:
                config_pairs.append(f"model={args.model}")
            if not args.strip_api_keys:
                config_pairs.append("strip-api-keys=false")
            actor = cmd_new(
                db, git,
                name=args.name,
                dir=args.dir,
                no_worktree=args.no_worktree,
                base=args.base,
                agent_name=args.agent,
                config_pairs=config_pairs,
            )
            print(f"{actor.name} created ({actor.dir})")

        elif args.command == "run":
            # Validate creation-only flags
            creation_only = {"dir": args.dir, "base": args.base}
            if args.no_worktree:
                creation_only["no-worktree"] = True
            for flag, val in creation_only.items():
                if val and not args.create:
                    raise ActorError(f"--{flag} requires -c/--create")

            # Build config pairs from flags
            config_pairs = list(args.config)
            if args.model is not None:
                config_pairs.append(f"model={args.model}")
            if args.strip_api_keys is not None:
                config_pairs.append(f"strip-api-keys={'true' if args.strip_api_keys else 'false'}")

            # Create actor if requested
            if args.create:
                cmd_new(
                    db, git,
                    name=args.name,
                    dir=args.dir,
                    no_worktree=args.no_worktree,
                    base=args.base,
                    agent_name=args.agent,
                    config_pairs=config_pairs,
                )

            # Interactive mode
            if args.interactive:
                actor = db.get_actor(args.name)
                session_id = actor.agent_session
                if session_id is None:
                    raise ActorError(f"'{args.name}' has no session yet — run it non-interactively first")
                os.chdir(actor.dir)
                if actor.agent == AgentKind.CLAUDE:
                    cmd = ["claude", "--resume", session_id]
                elif actor.agent == AgentKind.CODEX:
                    cmd = ["codex", "resume", session_id]
                else:
                    raise ActorError(f"interactive mode not supported for agent: {actor.agent}")
                os.execvp(cmd[0], cmd)

            # Resolve prompt: argument, stdin, or error
            prompt = args.prompt
            if prompt is None and not sys.stdin.isatty():
                prompt = sys.stdin.read().strip()
            if not prompt:
                print("error: prompt is required (pass as argument or pipe via stdin, or use -i)", file=sys.stderr)
                sys.exit(1)

            agent = agent_for(args.name)
            cmd_run(
                db, agent, proc_mgr,
                name=args.name,
                prompt=prompt,
                config_pairs=config_pairs if not args.create else [],
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
