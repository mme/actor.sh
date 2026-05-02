from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .errors import ActorError, ConfigError
from .interfaces import Agent
from .types import ActorConfig, AgentKind, Status, parse_config
from .db import Database
from .git import RealGit
from .process import RealProcessManager
from .commands import (
    _agent_class,
    _create_agent as _create_agent_impl,
    cmd_config,
    cmd_discard,
    cmd_interactive,
    cmd_list,
    cmd_logs,
    cmd_new,
    cmd_roles,
    cmd_run,
    cmd_show,
    cmd_stop,
)


def _create_agent(kind: AgentKind) -> Agent:
    # Thin re-export of commands._create_agent so existing test patches and
    # sibling modules (server.py, watch.app) can keep importing from `cli`.
    return _create_agent_impl(kind)


def _resolve_agent_kind_for_cli(
    cli_agent: Optional[str],
    role_name: Optional[str],
    app_config,
) -> AgentKind:
    """Replicate cmd_new's agent resolution for CLI-side validation.

    CLI validation of `--config` needs the target agent class to know which
    keys are actor-keys. Agent precedence mirrors cmd_new: explicit flag →
    role's `agent` → "claude"."""
    if cli_agent is not None:
        return AgentKind.from_str(cli_agent)
    if role_name is not None and app_config is not None:
        role = app_config.roles.get(role_name)
        if role is not None and role.agent:
            return AgentKind.from_str(role.agent)
    return AgentKind.CLAUDE


def _build_cli_overrides(
    agent_cls,
    config_pairs: list[str],
    use_subscription: Optional[bool] = None,
) -> ActorConfig:
    """Translate raw CLI inputs into a structured ActorConfig.

    `--config KEY=VALUE` always targets agent_args; if KEY collides with an
    actor-key name (i.e. appears in the agent class's ACTOR_DEFAULTS), we
    reject here with a helpful error pointing users at the dedicated flag.
    Dedicated actor-key flags (currently just `--use-subscription` /
    `--no-use-subscription`) populate actor_keys directly."""
    agent_args = parse_config(config_pairs)
    for key in agent_args:
        if key in agent_cls.ACTOR_DEFAULTS:
            # Channel-agnostic: same validation runs for CLI `--config` and
            # MCP `config=[...]`. Name both dedicated entrypoints so the
            # message is useful regardless of caller.
            param = key.replace("-", "_")
            raise ConfigError(
                f"{key} is an actor-key and cannot be set via --config / config=[...]; "
                f"use --{key} / --no-{key} (CLI) or {param}=true/false (MCP) instead."
            )
    actor_keys: dict[str, str] = {}
    if use_subscription is not None:
        actor_keys["use-subscription"] = "true" if use_subscription else "false"
    return ActorConfig(actor_keys=actor_keys, agent_args=agent_args)


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
  actor new my-feature --no-use-subscription        Pass API keys to the agent
  actor new my-feature --no-worktree                Use current directory directly
  actor new my-feature --dir /path/to/repo          Worktree from another repo
  actor new my-feature --base develop               Branch off develop
  actor new my-feature --config effort=max          Set agent config at creation
  actor new my-feature --role qa                    Apply the 'qa' role from settings.kdl
  echo "fix it" | actor new my-feature              Create and run with piped prompt""",
    )
    p_new.add_argument("name", help="Actor name")
    p_new.add_argument("prompt", nargs="?", default=None, help="Optional prompt to run immediately after creation")
    p_new.add_argument("--dir", default=None, help="Base directory (defaults to CWD)")
    p_new.add_argument("--no-worktree", action="store_true", help="Skip worktree creation, run in the directory directly")
    p_new.add_argument("--base", default=None, help="Branch to create the worktree from (defaults to current branch)")
    p_new.add_argument("--agent", default=None, help="Coding agent to use (defaults to role's agent or 'claude')")
    p_new.add_argument("--role", default=None, help="Apply a role from settings.kdl (see `actor roles` for available names)")
    p_new.add_argument("--model", default=None, help="Model for the agent to use")
    # Tri-state: default None = "no CLI override" so lower precedence layers
    # (role, kdl defaults block, class default) supply the value. Explicit
    # --use-subscription / --no-use-subscription force True/False as the
    # highest-precedence (CLI) layer.
    p_new.add_argument("--use-subscription", action="store_const", const=True, default=None, dest="use_subscription", help="Use the subscription by stripping API keys from the environment (overrides lower layers)")
    p_new.add_argument("--no-use-subscription", action="store_const", const=False, dest="use_subscription", help="Pass API keys through to the agent (overrides lower layers)")
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

    # -- roles --
    sub.add_parser(
        "roles",
        help="List available roles from settings.kdl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Roles are named presets defined in ~/.actor/settings.kdl (user) or
<repo>/.actor/settings.kdl (project). Apply one with `actor new <name>
--role <role>`.""",
    )

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
  actor discard my-feature                          Remove actor from DB
  actor discard my-feature --force                  Ignore an on-discard hook failure""",
    )
    p_discard.add_argument("name", help="Actor name")
    p_discard.add_argument(
        "-f", "--force",
        action="store_true",
        help="Bypass on-discard hook failures (actor is discarded even if the hook exits non-zero)",
    )

    # -- setup --
    p_setup = sub.add_parser(
        "setup",
        help="Install/reinstall integrations: the actor skill + MCP with a coding agent, or the omarchy theme-set hook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  actor setup --for claude-code                     User-wide install
  actor setup --for claude-code --scope project     Project-local install
  actor setup --for claude-code --name actor-dev    Register under a different name
  actor setup --for omarchy                         Install omarchy theme-set hook for instant TUI re-theme
  actor setup --for omarchy --uninstall             Remove the omarchy hook

'setup' is idempotent — safe to re-run. For a lightweight refresh of
just the skill files after upgrading actor-sh, use 'actor update'.""",
    )
    p_setup.add_argument("--for", dest="for_host", required=True, metavar="HOST", help="Integration target (claude-code, omarchy)")
    p_setup.add_argument("--scope", default="user", choices=["user", "project", "local"], help="Where to install (default: user). Ignored for --for omarchy.")
    p_setup.add_argument("--name", default="actor", help="Name to register the MCP under (default: actor). Ignored for --for omarchy.")
    p_setup.add_argument("--uninstall", action="store_true", help="Remove a previously installed integration (currently only supported for --for omarchy)")

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

    # -- main --
    p_main = sub.add_parser(
        "main",
        help="Launch the orchestrator session (claude with the `main` role applied)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Loads the resolved `main` role from settings.kdl and launches its agent
with the role's prompt appended as a system prompt and the actor channel
enabled. Trailing arguments are forwarded to the agent CLI verbatim.

The built-in `main` role ships as the Master Orchestrator; override it
with a `role "main" { ... }` block in settings.kdl to swap in your own
prompt or agent.

Examples:
  actor main                                        Open an orchestrator session
  actor main "kick off the refactor"                Non-interactive one-shot
  actor main --model opus                           Forward flags to the agent CLI""",
    )
    p_main.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to the agent CLI")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    effective_argv = sys.argv[1:] if argv is None else argv
    # `actor main ...` execs the agent CLI with the main role's prompt
    # appended as a system prompt and the actor channel enabled. Short-
    # circuit before argparse so unknown agent flags (--model, -p, etc.)
    # forwarded after `main` don't trip the top-level parser.
    if effective_argv and effective_argv[0] == "main":
        from .config import load_config
        cfg = load_config()
        role = cfg.roles.get("main")
        if role is None:
            print(
                "error: built-in `main` role missing — broken install? "
                "Reinstall actor-sh.",
                file=sys.stderr,
            )
            sys.exit(1)
        agent = role.agent or "claude"
        if agent != "claude":
            print(
                f"error: `actor main` only supports the claude agent, but "
                f"the resolved `main` role uses '{agent}'. Override the "
                f"main role's `agent` to \"claude\" in settings.kdl, or "
                f"launch the other agent CLI directly.",
                file=sys.stderr,
            )
            sys.exit(1)
        cmd = ["claude", "--dangerously-load-development-channels", "server:actor"]
        if role.prompt:
            cmd += ["--append-system-prompt", role.prompt]
        cmd += effective_argv[1:]
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
                uninstall=args.uninstall,
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

    # 'main' subcommand is short-circuited above before argparse runs;
    # this block is unreachable but kept for clarity if anything routes
    # back into argparse with command == "main".

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

            # --config goes into agent_args; --model is an agent_arg too.
            # --use-subscription is an actor-key, so it routes separately.
            config_pairs = list(args.config)
            if args.model is not None:
                config_pairs.append(f"model={args.model}")

            agent_kind = _resolve_agent_kind_for_cli(
                args.agent, args.role, app_config,
            )
            agent_cls = _agent_class(agent_kind)
            cli_overrides = _build_cli_overrides(
                agent_cls,
                config_pairs,
                use_subscription=args.use_subscription,
            )

            actor = cmd_new(
                db, git,
                name=args.name,
                dir=args.dir,
                no_worktree=args.no_worktree,
                base=args.base,
                agent_name=args.agent,
                cli_overrides=cli_overrides,
                role_name=args.role,
                app_config=app_config,
                hook_runner=None,
            )
            print(f"{actor.name} created ({actor.dir})")

            prompt = args.prompt
            stdin_consumed = False
            if prompt is None and not sys.stdin.isatty():
                prompt = sys.stdin.read().strip()
                stdin_consumed = True
            # Role prompt fallback runs before the empty-stdin check so
            # that `echo "" | actor new foo --role qa` uses the role's
            # prompt instead of erroring.
            if not prompt and args.role is not None:
                role = app_config.roles.get(args.role)
                if role is not None and role.prompt:
                    prompt = role.prompt
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
                        cli_overrides=ActorConfig(),  # creation flags already saved as defaults
                        app_config=app_config,
                    )
                except Exception as e:
                    print(f"error: actor created but run failed: {e}", file=sys.stderr)
                    sys.exit(2)

        elif args.command == "run":
            from .config import load_config as _load_config_run
            app_config_run = _load_config_run()
            # Interactive mode
            if args.interactive:
                agent = agent_for(args.name)
                exit_code, msg = cmd_interactive(
                    db, agent, proc_mgr, name=args.name,
                    app_config=app_config_run,
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

            actor_row = db.get_actor(args.name)
            agent_cls = _agent_class(actor_row.agent)
            cli_overrides = _build_cli_overrides(agent_cls, list(args.config))
            agent = _create_agent(actor_row.agent)
            cmd_run(
                db, agent, proc_mgr,
                name=args.name,
                prompt=prompt,
                cli_overrides=cli_overrides,
                app_config=app_config_run,
            )

        elif args.command == "list":
            output = cmd_list(db, proc_mgr, status_filter=args.status)
            print(output, end="")

        elif args.command == "roles":
            from .config import load_config
            output = cmd_roles(load_config())
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
            from .config import load_config as _load_config_discard
            msg = cmd_discard(
                db, proc_mgr, name=args.name,
                app_config=_load_config_discard(),
                force=args.force,
            )
            print(msg)

    except ActorError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
