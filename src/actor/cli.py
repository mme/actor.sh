from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .errors import ActorError
from .interfaces import Agent
from .types import AgentKind, Status
from .db import Database
from .git import RealGit
from .process import RealProcessManager
from .agents.claude import ClaudeAgent
from .agents.codex import CodexAgent
from .commands import (
    cmd_config,
    cmd_discard,
    cmd_interactive,
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
        description="Manage coding agents in parallel",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"actor-sh {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    # -- new --
    p_new = sub.add_parser(
        "new",
        help="Create a new actor (optionally run a prompt immediately)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor new my-feature                              Create (worktree from current repo)
  actor new my-feature "fix the nav bar"            Create and run with a prompt
  actor new my-feature --model sonnet               Use a specific model
  actor new my-feature --no-strip-api-keys          Pass API keys to the agent
  actor new my-feature --no-worktree                Use current directory directly
  actor new my-feature --dir /path/to/repo          Worktree from another repo
  actor new my-feature --base develop               Branch off develop
  actor new my-feature --config effort=max          Set agent config at creation
  actor new my-feature --template qa                Apply the 'qa' template from settings.kdl
  echo "fix it" | actor new my-feature              Create and run with piped prompt""",
    )
    p_new.add_argument("name", help="Actor name")
    p_new.add_argument("prompt", nargs="?", default=None, help="Optional prompt to run immediately after creation")
    p_new.add_argument("--dir", default=None, help="Base directory (defaults to CWD)")
    p_new.add_argument("--no-worktree", action="store_true", help="Skip worktree creation, run in the directory directly")
    p_new.add_argument("--base", default=None, help="Branch to create the worktree from (defaults to current branch)")
    p_new.add_argument("--agent", default=None, help="Coding agent to use (defaults to template's agent or 'claude')")
    p_new.add_argument("--template", default=None, help="Apply a template from settings.kdl")
    p_new.add_argument("--model", default=None, help="Model for the agent to use")
    # Tri-state: default None = "no override" so a template's strip-api-keys
    # value wins. Explicit --strip-api-keys / --no-strip-api-keys set True/
    # False and beat the template. When neither CLI nor template sets the
    # key, it's omitted from config and the agent's own default applies
    # (ClaudeAgent / CodexAgent both treat a missing key as "strip").
    p_new.add_argument("--strip-api-keys", action="store_const", const=True, default=None, dest="strip_api_keys", help="Strip API keys from environment (default)")
    p_new.add_argument("--no-strip-api-keys", action="store_const", const=False, dest="strip_api_keys", help="Pass API keys through to the agent")
    p_new.add_argument("--config", dest="config", action="append", default=[], metavar="KEY=VALUE", help="Config key=value pair (repeat for multiple)")

    # -- run --
    p_run = sub.add_parser(
        "run",
        help="Run an existing actor with a prompt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor run fix-nav "continue fixing"                Run with a prompt
  actor run fix-nav --config model=opus "one-off"    Temporary config override for this run
  actor run fix-nav -i                               Resume interactively
  echo "fix it" | actor run fix-nav                  Prompt from stdin

To create an actor, use 'actor new'. To change actor defaults, use 'actor config'.""",
    )
    p_run.add_argument("name", help="Actor name")
    p_run.add_argument("prompt", nargs="?", default=None, help="Prompt (reads stdin if omitted and not interactive)")
    p_run.add_argument("-i", "--interactive", action="store_true", help="Resume the actor in interactive mode")
    p_run.add_argument("--config", dest="config", action="append", default=[], metavar="KEY=VALUE", help="Per-run config override key=value (repeat for multiple, not saved to actor)")

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

    # -- mcp --
    p_mcp = sub.add_parser(
        "mcp",
        help="Start MCP server (stdio transport, used by Claude Code)",
    )
    p_mcp.add_argument("--for", dest="for_host", default=None, metavar="HOST", help="Coding agent host this server is serving (e.g. claude-code, codex)")

    # -- watch --
    p_watch = sub.add_parser(
        "watch",
        help="Open real-time dashboard (browser + terminal)",
    )
    p_watch.add_argument("--serve", action="store_true", help="Serve in browser via textual-serve on port 2204")
    p_watch.add_argument("--no-animation", action="store_true", help="Disable splash animation (lighter over SSH/slow links)")

    # -- discard --
    p_discard = sub.add_parser(
        "discard",
        help="Remove an actor from the database (worktree stays on disk)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor discard my-feature                          Remove actor from DB""",
    )
    p_discard.add_argument("name", help="Actor name")

    # -- setup --
    p_setup = sub.add_parser(
        "setup",
        help="Install or reinstall the actor skill + register the MCP with a coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor setup --for claude-code                     User-wide install
  actor setup --for claude-code --scope project     Project-local install
  actor setup --for claude-code --name actor-dev    Register under a different name

'setup' is idempotent — safe to re-run. For a lightweight refresh of
just the skill files after upgrading actor-sh, use 'actor update'.""",
    )
    p_setup.add_argument("--for", dest="for_host", required=True, metavar="HOST", help="Coding agent host (currently supported: claude-code)")
    p_setup.add_argument("--scope", default="user", choices=["user", "project", "local"], help="Where to install (default: user)")
    p_setup.add_argument("--name", default="actor", help="Name to register the MCP under (default: actor)")

    # -- update --
    p_update = sub.add_parser(
        "update",
        help="Refresh the deployed actor skill files to match the installed actor-sh version",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor update                                       Refresh user-wide install
  actor update --scope project                       Refresh project-local install""",
    )
    p_update.add_argument("--for", dest="for_host", default="claude-code", metavar="HOST", help="Coding agent host (default: claude-code)")
    p_update.add_argument("--scope", default="user", choices=["user", "project", "local"], help="Which install to refresh (default: user)")
    p_update.add_argument("--name", default="actor", help="MCP name used at setup time (default: actor)")

    # -- claude --
    p_claude = sub.add_parser(
        "claude",
        help="Launch Claude Code with the actor channel enabled",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
All arguments are passed through to the claude CLI verbatim.
Equivalent to: claude --dangerously-load-development-channels server:actor <args>

Examples:
  actor claude                                      Open an interactive session
  actor claude "fix the nav bar"                    Non-interactive one-shot
  actor claude --model opus                         Forward flags to claude""",
    )
    p_claude.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to the claude CLI")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    effective_argv = sys.argv[1:] if argv is None else argv
    # `actor claude ...` forwards everything after to the claude CLI verbatim.
    # Short-circuit before argparse so unknown claude flags (--model, -p, etc.)
    # don't trip the top-level parser.
    if effective_argv and effective_argv[0] == "claude":
        cmd = [
            "claude", "--dangerously-load-development-channels", "server:actor",
            *effective_argv[1:],
        ]
        try:
            os.execvp(cmd[0], cmd)
        except FileNotFoundError:
            print(
                "error: `claude` CLI not found on PATH. Install Claude Code first "
                "(https://claude.com/claude-code).",
                file=sys.stderr,
            )
            sys.exit(1)
        # execvp replaces the process on success, so control only reaches here
        # if the call was mocked (in tests). Don't fall through to argparse.
        return

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "mcp":
        from .server import main as mcp_main
        mcp_main(for_host=args.for_host)
        return

    if args.command == "watch":
        from .watch import run_watch
        run_watch(serve=args.serve, animate=not args.no_animation)
        return

    if args.command == "setup":
        from .setup import cmd_setup
        try:
            msg = cmd_setup(
                for_host=args.for_host,
                scope=args.scope,
                name=args.name,
            )
            print(msg)
        except ActorError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "update":
        from .setup import cmd_update
        try:
            msg = cmd_update(
                for_host=args.for_host,
                scope=args.scope,
                name=args.name,
            )
            print(msg)
        except ActorError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # 'claude' subcommand is short-circuited above before argparse runs;
    # this block is unreachable but kept for clarity if anything routes
    # back into argparse with command == "claude".

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
            from .config import load_config
            app_config = load_config()

            config_pairs = list(args.config)
            if args.model is not None:
                config_pairs.append(f"model={args.model}")
            if args.strip_api_keys is not None:
                config_pairs.append(
                    f"strip-api-keys={'true' if args.strip_api_keys else 'false'}"
                )
            actor = cmd_new(
                db, git,
                name=args.name,
                dir=args.dir,
                no_worktree=args.no_worktree,
                base=args.base,
                agent_name=args.agent,
                config_pairs=config_pairs,
                template_name=args.template,
                app_config=app_config,
            )
            print(f"{actor.name} created ({actor.dir})")

            prompt = args.prompt
            stdin_consumed = False
            if prompt is None and not sys.stdin.isatty():
                prompt = sys.stdin.read().strip()
                stdin_consumed = True
            # Template prompt fallback runs before the empty-stdin check so
            # that `echo "" | actor new foo --template qa` uses the template's
            # prompt instead of erroring.
            if not prompt and args.template is not None:
                tpl = app_config.templates.get(args.template)
                if tpl is not None and tpl.prompt:
                    prompt = tpl.prompt
            if stdin_consumed and not prompt:
                print("error: stdin was empty — expected a prompt", file=sys.stderr)
                sys.exit(1)
            if prompt:
                try:
                    agent = agent_for(args.name)
                    cmd_run(
                        db, agent, proc_mgr,
                        name=args.name,
                        prompt=prompt,
                        config_pairs=[],  # creation flags already saved as defaults
                    )
                except Exception as e:
                    print(f"error: actor created but run failed: {e}", file=sys.stderr)
                    sys.exit(2)

        elif args.command == "run":
            # Interactive mode
            if args.interactive:
                agent = agent_for(args.name)
                exit_code, msg = cmd_interactive(
                    db, agent, proc_mgr, name=args.name,
                )
                print(msg, file=sys.stderr)
                # POSIX convention for signal termination: 128 + signum.
                # cmd_interactive returns -signum in that case.
                if exit_code < 0:
                    sys.exit(128 - exit_code)
                sys.exit(exit_code)

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
                config_pairs=list(args.config),
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

        elif args.command == "discard":
            msg = cmd_discard(db, proc_mgr, name=args.name)
            print(msg)

    except ActorError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
