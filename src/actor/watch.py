"""actor watch — real-time dashboard for actor.sh."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.content import Content
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from .db import Database
from .process import RealProcessManager
from .types import Actor, Status
from .cli import _db_path


# -- Status icons -----------------------------------------------------------

STATUS_ICON = {
    Status.RUNNING: "●",
    Status.DONE: "○",
    Status.ERROR: "✗",
    Status.IDLE: "◌",
    Status.STOPPED: "■",
}


# -- Helper: build tree display ---------------------------------------------

def _build_tree(actors: list[Actor], statuses: dict[str, Status]) -> list[tuple[str, Actor]]:
    """Build a display list with tree indentation. Returns (display_prefix, actor) pairs."""
    actor_names = {a.name for a in actors}

    # Detect which actors are reachable from a None root.
    # Actors in cycles or whose ancestor chain never reaches None are promoted to top-level.
    def _find_root(a: Actor) -> str | None:
        """Walk parent chain; return None if it reaches a root, actor name if it cycles."""
        seen: set[str] = set()
        cur = a.parent
        while cur is not None and cur in actor_names:
            if cur in seen:
                return a.name  # cycle detected
            seen.add(cur)
            parent_actor = next((x for x in actors if x.name == cur), None)
            cur = parent_actor.parent if parent_actor else None
        return None  # reached a true root

    by_parent: dict[str | None, list[Actor]] = {}
    for a in actors:
        parent = a.parent if a.parent in actor_names else None
        # Break cycles: if this actor's ancestor chain cycles, treat it as top-level
        if parent is not None and _find_root(a) is not None:
            parent = None
        by_parent.setdefault(parent, []).append(a)

    # Sort: running first, then by created_at
    def sort_key(a: Actor) -> tuple[int, str]:
        s = statuses.get(a.name, Status.IDLE)
        order = {Status.RUNNING: 0, Status.ERROR: 1, Status.IDLE: 2, Status.DONE: 3, Status.STOPPED: 4}
        return (order.get(s, 9), a.created_at or "")

    result: list[tuple[str, Actor]] = []
    visited: set[str] = set()

    def _walk(parent: str | None, is_last_stack: list[bool]) -> None:
        children = by_parent.get(parent, [])
        children.sort(key=sort_key)
        for i, child in enumerate(children):
            if child.name in visited:
                continue  # prevent cycles
            visited.add(child.name)
            is_last = i == len(children) - 1
            if parent is None:
                display_prefix = ""
            else:
                connector = "└─ " if is_last else "├─ "
                indent = ""
                for il in is_last_stack[:-1]:
                    indent += "   " if il else "│  "
                display_prefix = indent + connector
            result.append((display_prefix, child))
            _walk(child.name, is_last_stack + [is_last])

    _walk(None, [])
    return result


# -- Helper: read logs ------------------------------------------------------

def _read_logs(actor: Actor, verbose: bool = False) -> str:
    """Read actor session logs. Returns formatted text."""
    from .agents.claude import ClaudeAgent
    from .agents.codex import CodexAgent
    from .commands import cmd_logs
    from .interfaces import Agent

    agent: Agent
    from .types import AgentKind
    if actor.agent == AgentKind.CLAUDE:
        agent = ClaudeAgent()
    else:
        agent = CodexAgent()

    db = Database.open(_db_path())
    try:
        return cmd_logs(db, agent, name=actor.name, verbose=verbose, watch=False)
    except Exception:
        return ""


# -- Helper: compute git diff -----------------------------------------------

def _compute_diff(actor: Actor) -> tuple[str, str, str, str] | None:
    """Compute diff for an actor. Returns (path_orig, path_mod, orig_content, mod_content) or None."""
    if not actor.source_repo or not actor.base_branch or not actor.worktree:
        return None

    worktree_dir = actor.dir

    try:
        # Get changed files: committed + uncommitted vs base branch
        result = subprocess.run(
            ["git", "diff", "--name-only", actor.base_branch],
            capture_output=True, text=True, cwd=worktree_dir,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        files = result.stdout.strip().split("\n")

        orig_parts = []
        mod_parts = []
        for f in files:
            # Original from base branch
            orig_result = subprocess.run(
                ["git", "show", f"{actor.base_branch}:{f}"],
                capture_output=True, text=True, cwd=worktree_dir,
            )
            orig_parts.append(f"# {f}\n" + (orig_result.stdout if orig_result.returncode == 0 else ""))

            # Modified: read from working tree (includes uncommitted changes)
            file_path = Path(worktree_dir) / f
            try:
                mod_content = file_path.read_text()
            except (FileNotFoundError, OSError):
                mod_content = ""
            mod_parts.append(f"# {f}\n" + mod_content)

        return (
            actor.base_branch,
            f"{actor.name} (working tree)",
            "\n".join(orig_parts),
            "\n".join(mod_parts),
        )
    except Exception:
        return None


# -- Actor List Widget -------------------------------------------------------

class ActorList(Vertical):
    """Left panel showing all actors with tree indentation."""

    selected_index: reactive[int] = reactive(0)

    def __init__(self) -> None:
        super().__init__(id="actor-list")
        self._entries: list[tuple[str, Actor]] = []
        self._statuses: dict[str, Status] = {}

    def compose(self) -> ComposeResult:
        yield Static("ACTORS", classes="panel-title")
        yield Static("", id="actor-list-content")

    def update_actors(self, actors: list[Actor], statuses: dict[str, Status]) -> None:
        self._statuses = statuses
        self._entries = _build_tree(actors, statuses)
        self._render_list()

    def _render_list(self) -> None:
        lines = []
        for i, (prefix, actor) in enumerate(self._entries):
            status = self._statuses.get(actor.name, Status.IDLE)
            icon = STATUS_ICON.get(status, "?")
            marker = ">" if i == self.selected_index else " "
            lines.append(f"{marker} {icon} {prefix}{actor.name}")
        content = self.query_one("#actor-list-content", Static)
        content.update("\n".join(lines) if lines else "(no actors)")

    def watch_selected_index(self, new_value: int) -> None:
        self._render_list()

    def move_up(self) -> None:
        if self._entries and self.selected_index > 0:
            self.selected_index -= 1

    def move_down(self) -> None:
        if self._entries and self.selected_index < len(self._entries) - 1:
            self.selected_index += 1

    @property
    def selected_actor(self) -> Actor | None:
        if 0 <= self.selected_index < len(self._entries):
            return self._entries[self.selected_index][1]
        return None


# -- Main App ----------------------------------------------------------------

class ActorWatchApp(App):
    """Real-time dashboard for actor.sh."""

    CSS = """
    Screen {
        background: transparent;
    }
    #actor-list {
        width: 28;
        border-right: solid gray;
        background: transparent;
    }
    .panel-title {
        text-style: bold;
        padding: 0 1;
    }
    .actor-entry {
        padding: 0 1;
    }
    .actor-entry.selected {
        text-style: reverse bold;
    }
    .status-running {
        color: green;
    }
    .status-done {
        color: gray;
    }
    .status-error {
        color: red;
    }
    .status-idle {
        color: gray;
    }
    .status-stopped {
        color: yellow;
    }
    #detail-panel {
        width: 1fr;
        background: transparent;
    }
    #logs-content {
        padding: 1;
    }
    #diff-content {
        padding: 1;
    }
    #info-content {
        padding: 1;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
    }
    TabbedContent {
        background: transparent;
    }
    TabPane {
        background: transparent;
    }
    VerticalScroll {
        background: transparent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("k,up", "move_up", "Up", show=False),
        Binding("j,down", "move_down", "Down", show=False),
        Binding("left", "noop", "", show=False, priority=True),
        Binding("right", "noop", "", show=False, priority=True),
        Binding("ctrl+p", "command_palette", "Command Palette"),
        Binding("l", "show_tab('logs')", "Logs"),
        Binding("d", "show_tab('diff')", "Diff"),
        Binding("r", "show_tab('runs')", "Runs"),
        Binding("i", "show_tab('info')", "Info"),
        Binding("v", "toggle_verbose", "Verbose", show=False),
    ]

    verbose: reactive[bool] = reactive(False)
    _prev_statuses: dict[str, Status] = {}
    _current_actors: list[Actor] = []
    _diff_loaded_for: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield ActorList()
            with Vertical(id="detail-panel"):
                with TabbedContent(id="tabs"):
                    hl = "$footer-key-foreground on $footer-key-background bold"
                    with TabPane(Content.from_markup(f"[{hl}]L[/]ogs"), id="logs"):
                        yield VerticalScroll(
                            Static("Select an actor", id="logs-content"),
                        )
                    with TabPane(Content.from_markup(f"[{hl}]D[/]iff"), id="diff"):
                        yield VerticalScroll(id="diff-scroll")
                    with TabPane(Content.from_markup(f"[{hl}]R[/]uns"), id="runs"):
                        yield VerticalScroll(
                            DataTable(id="runs-table"),
                        )
                    with TabPane(Content.from_markup(f"[{hl}]I[/]nfo"), id="info"):
                        yield VerticalScroll(
                            Static("Select an actor", id="info-content"),
                        )
        yield Static("Loading...", id="status-bar")
        yield Footer()

    def on_ready(self) -> None:
        # Do first poll synchronously so actors are visible immediately
        actors, statuses = self._fetch_actors()
        self._update_ui(actors, statuses)
        self.set_interval(2.0, self._poll_actors_async)

    @staticmethod
    def _fetch_actors() -> tuple[list[Actor], dict[str, Status]]:
        db = Database.open(_db_path())
        pm = RealProcessManager()
        actors = db.list_actors()
        statuses = {}
        for a in actors:
            statuses[a.name] = db.resolve_actor_status(a.name, pm)
        return actors, statuses

    @work(thread=True)
    def _poll_actors_async(self) -> None:
        actors, statuses = self._fetch_actors()
        self.call_from_thread(self._update_ui, actors, statuses)

    def _update_ui(self, actors: list[Actor], statuses: dict[str, Status]) -> None:
        # Detect status changes for toasts
        for a in actors:
            new_status = statuses.get(a.name)
            old_status = self._prev_statuses.get(a.name)
            if old_status and new_status and old_status != new_status:
                if new_status == Status.DONE:
                    self.notify(f"✓ {a.name} done", severity="information")
                elif new_status == Status.ERROR:
                    self.notify(f"✗ {a.name} error", severity="error")

        self._prev_statuses = dict(statuses)
        self._current_actors = actors

        # Update actor list
        actor_list = self.query_one(ActorList)
        actor_list.update_actors(actors, statuses)

        # Update status bar
        running = sum(1 for s in statuses.values() if s == Status.RUNNING)
        done = sum(1 for s in statuses.values() if s == Status.DONE)
        errors = sum(1 for s in statuses.values() if s == Status.ERROR)
        total = len(actors)
        status_bar = self.query_one("#status-bar", Static)
        status_bar.update(
            f" {total} actors: {running} running, {done} done, {errors} error"
            f"{'  ' * 10}localhost:2204"
        )

        # Refresh detail for selected actor
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        actor_list = self.query_one(ActorList)
        actor = actor_list.selected_actor
        if actor is None:
            return

        # Update info tab
        status = self._prev_statuses.get(actor.name, Status.IDLE)
        info = self.query_one("#info-content", Static)
        config_str = "\n".join(f"  {k}={v}" for k, v in sorted(actor.config.items())) if actor.config else "  (none)"
        info.update(
            f"Name:      {actor.name}\n"
            f"Agent:     {actor.agent.value}\n"
            f"Status:    {status.value}\n"
            f"Dir:       {actor.dir}\n"
            f"Source:    {actor.source_repo or '—'}\n"
            f"Base:      {actor.base_branch or '—'}\n"
            f"Parent:    {actor.parent or '—'}\n"
            f"Session:   {actor.agent_session or '—'}\n"
            f"Created:   {actor.created_at}\n"
            f"Config:\n{config_str}"
        )

        # Update runs tab
        self._refresh_runs(actor)

        # Update logs
        self._refresh_logs(actor)

        # Diff is only refreshed on actor change or explicit tab switch, not every poll

    @work(thread=True)
    def _refresh_logs(self, actor: Actor) -> None:
        log_text = _read_logs(actor, verbose=self.verbose)
        self.call_from_thread(self._set_logs, log_text or "No logs yet")

    def _set_logs(self, text: str) -> None:
        logs = self.query_one("#logs-content", Static)
        logs.update(text)

    def _maybe_refresh_diff(self, force: bool = False) -> None:
        actor = self.query_one(ActorList).selected_actor
        if actor is None:
            return
        if not force and self._diff_loaded_for == actor.name:
            return
        self._diff_loaded_for = actor.name
        self._refresh_diff(actor)

    @work(thread=True, exclusive=True, group="diff")
    def _refresh_diff(self, actor: Actor) -> None:
        from textual_diff_view import DiffView

        diff_data = _compute_diff(actor)

        if diff_data is None:
            self.call_from_thread(self._set_diff_text, "No diff available (no worktree or no changes)")
            return

        path_orig, path_mod, orig, mod = diff_data
        if orig == mod:
            self.call_from_thread(self._set_diff_text, "No changes")
            return

        try:
            dv = DiffView(
                path_original=path_orig,
                path_modified=path_mod,
                code_original=orig,
                code_modified=mod,
            )
            # Do expensive diff/highlight work in this thread, not the UI thread
            dv.grouped_opcodes
            dv.highlighted_code_lines
            self.call_from_thread(self._set_diff_widget, dv)
        except Exception as e:
            self.call_from_thread(self._set_diff_text, f"Diff error: {e}")

    def _set_diff_text(self, text: str) -> None:
        scroll = self.query_one("#diff-scroll", VerticalScroll)
        scroll.remove_children()
        scroll.mount(Static(text))

    def _set_diff_widget(self, dv: object) -> None:
        scroll = self.query_one("#diff-scroll", VerticalScroll)
        scroll.remove_children()
        scroll.mount(dv)

    def _refresh_runs(self, actor: Actor) -> None:
        db = Database.open(_db_path())
        runs, _total = db.list_runs(actor.name, limit=50)
        table = self.query_one("#runs-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Status", "Exit", "Prompt", "Started", "Duration")
        for run in reversed(runs):
            duration = ""
            if run.started_at and run.finished_at:
                from .types import _parse_iso
                start = _parse_iso(run.started_at)
                end = _parse_iso(run.finished_at)
                if start and end:
                    secs = int((end - start).total_seconds())
                    duration = f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60}s"
            prompt_short = (run.prompt[:40] + "...") if len(run.prompt) > 43 else run.prompt
            table.add_row(
                str(run.id),
                run.status.value,
                str(run.exit_code) if run.exit_code is not None else "—",
                prompt_short,
                run.started_at or "—",
                duration or "—",
            )

    # -- Actions -------------------------------------------------------------

    def action_noop(self) -> None:
        pass

    def action_move_up(self) -> None:
        self.query_one(ActorList).move_up()
        self._refresh_detail()
        self._maybe_refresh_diff()

    def action_move_down(self) -> None:
        self.query_one(ActorList).move_down()
        self._refresh_detail()
        self._maybe_refresh_diff()

    def action_show_tab(self, tab_id: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = tab_id
        if tab_id == "diff":
            self._maybe_refresh_diff(force=True)

    def action_toggle_verbose(self) -> None:
        self.verbose = not self.verbose
        actor = self.query_one(ActorList).selected_actor
        if actor:
            self._refresh_logs(actor)


def run_watch(serve: bool = True) -> None:
    """Entry point for `actor watch`."""
    if serve:
        try:
            from textual_serve.server import Server
            server = Server("uv run python -m actor.watch --no-serve", port=2204)
            server.serve()
        except ImportError:
            # Fall back to terminal
            app = ActorWatchApp()
            app.run()
    else:
        app = ActorWatchApp()
        app.run()


def main() -> None:
    """Direct entry point when run as module."""
    import sys
    serve = "--no-serve" not in sys.argv
    run_watch(serve=serve)


if __name__ == "__main__":
    main()
