"""actor watch — real-time dashboard for actor.sh."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

from rich.color import Color as RichColor, ColorType
from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.panel import Panel
from rich.style import Style as RichStyle
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.filter import ANSIToTruecolor
from textual.reactive import reactive
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Footer,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from .db import Database
from .interfaces import LogEntryKind
from .process import RealProcessManager
from .types import Actor, Status
from .cli import _db_path


# -- Patch ANSIToTruecolor to preserve DEFAULT colors (SGR 49/39) -----------
# Without this, the filter converts RichColor("default") to a concrete RGB
# triplet, which prevents the terminal's own background from showing through.

_original_truecolor_style = ANSIToTruecolor.__dict__[
    "truecolor_style"
].__wrapped__


@lru_cache(1024)
def _patched_truecolor_style(
    self: ANSIToTruecolor, style: RichStyle, background: RichColor
) -> RichStyle:
    had_default_fg = style.color is not None and style.color.type == ColorType.DEFAULT
    had_default_bg = (
        style.bgcolor is not None and style.bgcolor.type == ColorType.DEFAULT
    )
    result = _original_truecolor_style(self, style, background)
    if had_default_fg or had_default_bg:
        overrides: dict[str, RichColor] = {}
        if had_default_fg:
            overrides["color"] = RichColor.parse("default")
        if had_default_bg:
            overrides["bgcolor"] = RichColor.parse("default")
        result = result + RichStyle(**overrides)
    return result


ANSIToTruecolor.truecolor_style = _patched_truecolor_style  # type: ignore[assignment]


# -- Status icons -----------------------------------------------------------

STATUS_ICON = {
    Status.RUNNING: "●",
    Status.DONE: "○",
    Status.ERROR: "✗",
    Status.IDLE: "◌",
    Status.STOPPED: "■",
}


# -- Helper: group actors by parent ------------------------------------------

def _group_by_parent(actors: list[Actor], statuses: dict[str, Status]) -> dict[str | None, list[Actor]]:
    """Group actors by parent, handling cycles and missing parents."""
    actor_names = {a.name for a in actors}

    def _has_cycle(a: Actor) -> bool:
        seen: set[str] = set()
        cur = a.parent
        while cur is not None and cur in actor_names:
            if cur in seen:
                return True
            seen.add(cur)
            parent_actor = next((x for x in actors if x.name == cur), None)
            cur = parent_actor.parent if parent_actor else None
        return False

    by_parent: dict[str | None, list[Actor]] = {}
    for a in actors:
        parent = a.parent if a.parent in actor_names else None
        if parent is not None and _has_cycle(a):
            parent = None
        by_parent.setdefault(parent, []).append(a)

    # Sort by most recent first, then stable-sort by status group
    def sort_key(a: Actor) -> int:
        s = statuses.get(a.name, Status.IDLE)
        order = {Status.RUNNING: 0, Status.ERROR: 1, Status.IDLE: 2, Status.DONE: 3, Status.STOPPED: 4}
        return order.get(s, 9)

    for children in by_parent.values():
        children.sort(key=lambda a: a.updated_at or a.created_at or "", reverse=True)
        children.sort(key=sort_key)

    return by_parent


# -- Helper: read log entries ------------------------------------------------

def _read_log_entries(actor: Actor) -> list:
    """Read raw LogEntry list for an actor."""
    from .agents.claude import ClaudeAgent
    from .agents.codex import CodexAgent
    from .interfaces import Agent, LogEntry

    agent: Agent
    from .types import AgentKind
    if actor.agent == AgentKind.CLAUDE:
        agent = ClaudeAgent()
    else:
        agent = CodexAgent()

    if actor.agent_session is None:
        return []
    try:
        return agent.read_logs(Path(actor.dir), actor.agent_session)
    except Exception:
        return []


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


# -- Actor Tree Widget -------------------------------------------------------

class ActorTree(Tree[Actor]):
    """Left panel showing all actors as a tree."""

    DEFAULT_CSS = """
    ActorTree {
        width: 28;
        border: blank;
        padding: 0 1;
    }
    ActorTree:focus {
        border: round $primary;
    }
    ActorTree > .tree--cursor {
        background: $primary;
        color: $text;
        text-style: bold;
    }
    """

    def __init__(self) -> None:
        super().__init__("Actors", id="actor-tree")
        self.show_root = False
        self.guide_depth = 3
        self._snapshot: dict[str, str] = {}  # name -> "icon name"

    def update_actors(self, actors: list[Actor], statuses: dict[str, Status]) -> None:
        # Build a snapshot of what the tree should look like
        new_snapshot: dict[str, str] = {}
        for a in actors:
            status = statuses.get(a.name, Status.IDLE)
            icon = STATUS_ICON.get(status, "?")
            new_snapshot[a.name] = f"{icon} {a.name}"

        # TODO: re-enable after testing cursor stability
        # if new_snapshot == self._snapshot:
        #     return

        # Check if only labels changed (same actors, same structure)
        if set(new_snapshot.keys()) == set(self._snapshot.keys()):
            # Same actors — just update labels in place
            self._update_labels(self.root, new_snapshot)
            self._snapshot = new_snapshot
            return

        # Structure changed — full rebuild needed
        selected_name = None
        if self.cursor_node and self.cursor_node.data:
            selected_name = self.cursor_node.data.name

        # Remember expanded state
        expanded: set[str] = set()
        self._collect_expanded(self.root, expanded)

        self.clear()
        self._snapshot = new_snapshot
        by_parent = _group_by_parent(actors, statuses)
        visited: set[str] = set()

        def _add_children(parent_node, parent_key: str | None) -> None:
            for actor in by_parent.get(parent_key, []):
                if actor.name in visited:
                    continue
                visited.add(actor.name)
                label = new_snapshot[actor.name]
                has_children = actor.name in by_parent
                if has_children:
                    should_expand = actor.name in expanded or actor.name not in self._snapshot
                    node = parent_node.add(label, data=actor, expand=should_expand)
                    _add_children(node, actor.name)
                else:
                    parent_node.add_leaf(label, data=actor)

        _add_children(self.root, None)

        # Restore selection, or select first item
        if selected_name:
            self._select_by_name(self.root, selected_name)
        elif self.root.children:
            self.select_node(self.root.children[0])

    def _update_labels(self, node, snapshot: dict[str, str]) -> None:
        """Update labels in place without rebuilding the tree."""
        for child in node.children:
            if child.data and child.data.name in snapshot:
                new_label = snapshot[child.data.name]
                if str(child.label) != new_label:
                    child.set_label(new_label)
            self._update_labels(child, snapshot)

    def _collect_expanded(self, node, expanded: set[str]) -> None:
        """Collect names of expanded nodes."""
        for child in node.children:
            if child.data and child.is_expanded:
                expanded.add(child.data.name)
            self._collect_expanded(child, expanded)

    def _select_by_name(self, node, name: str) -> bool:
        for child in node.children:
            if child.data and child.data.name == name:
                self.select_node(child)
                return True
            if self._select_by_name(child, name):
                return True
        return False
        return False

    @property
    def selected_actor(self) -> Actor | None:
        node = self.cursor_node
        if node and node.data:
            return node.data
        return None


# -- Themes ------------------------------------------------------------------

CLAUDE_DARK = Theme(
    name="claude-dark",
    primary="#B1B9F9",
    secondary="#D77757",
    warning="#FFC107",
    error="#FF6B80",
    success="#4EBA65",
    accent="#00CCCC",
    foreground="#FFFFFF",
    background="#1A1A1A",
    surface="#373737",
    panel="#2C323E",
    dark=True,
)

CLAUDE_LIGHT = Theme(
    name="claude-light",
    primary="#5769F7",
    secondary="#D77757",
    warning="#966C1E",
    error="#AB2B3F",
    success="#2C7A39",
    accent="#009999",
    foreground="#000000",
    background="#FFFFFF",
    surface="#F0F0F0",
    panel="#E8ECF4",
    dark=False,
)


# -- Main App ----------------------------------------------------------------

class ActorWatchApp(App):
    """Real-time dashboard for actor.sh."""

    CSS = """
    Screen, Tabs, Tab, TabbedContent, TabPane,
    ContentSwitcher, VerticalScroll, RichLog,
    DataTable, Tree, #detail-panel, #status-bar {
        background: ansi_default;
    }
    #detail-panel {
        width: 1fr;
        border: blank;
    }
    #detail-panel:focus-within {
        border: round $primary;
    }
    .underline--bar {
        background: $foreground 30%;
    }
    * {
        scrollbar-background: $foreground 30%;
    }
    #logs-content {
        padding: 0 1;
    }
    #info-content {
        padding: 1;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("left", "prev_tab", "Prev Tab", show=False, priority=True),
        Binding("right", "next_tab", "Next Tab", show=False, priority=True),
        Binding("p", "command_palette", "Palette"),
        Binding("l", "show_tab('logs')", "Logs"),
        Binding("d", "show_tab('diff')", "Diff"),
        Binding("r", "show_tab('runs')", "Runs"),
        Binding("i", "show_tab('info')", "Info"),
    ]

    _prev_statuses: dict[str, Status] = {}
    _current_actors: list[Actor] = []
    _diff_loaded_for: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield ActorTree()
            with Vertical(id="detail-panel"):
                with TabbedContent(id="tabs"):
                    with TabPane("Logs", id="logs"):
                        yield RichLog(id="logs-content", wrap=True, markup=False, auto_scroll=False)
                    with TabPane("Diff", id="diff"):
                        yield VerticalScroll(id="diff-scroll")
                    with TabPane("Runs", id="runs"):
                        yield VerticalScroll(
                            DataTable(id="runs-table"),
                        )
                    with TabPane("Info", id="info"):
                        yield VerticalScroll(
                            Static("Select an actor", id="info-content"),
                        )
        yield Static("Loading...", id="status-bar")
        yield Footer()

    def on_ready(self) -> None:
        self.register_theme(CLAUDE_DARK)
        self.register_theme(CLAUDE_LIGHT)
        self.theme = "claude-dark"

        # Only actor tree and detail content should be tab stops
        for widget in self.query("Tabs, Tab, Footer, DataTable"):
            widget.can_focus = False

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
        actor_list = self.query_one(ActorTree)
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
        actor_list = self.query_one(ActorTree)
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

    @work(thread=True, exclusive=True, group="logs")
    def _refresh_logs(self, actor: Actor) -> None:
        entries = _read_log_entries(actor)
        self.call_from_thread(self._set_logs, entries)

    _last_log_count: int = 0
    _last_log_width: int = 0
    _last_log_entries: list = []

    def on_resize(self) -> None:
        """Force log re-render on resize."""
        log = self.query_one("#logs-content", RichLog)
        if log.size.width != self._last_log_width and self._last_log_entries:
            self._last_log_count = 0  # force re-render
            self._set_logs(self._last_log_entries)

    def _set_logs(self, entries: list) -> None:
        log = self.query_one("#logs-content", RichLog)

        # Only re-render if entry count or width changed
        if len(entries) == self._last_log_count and log.size.width == self._last_log_width:
            return
        self._last_log_count = len(entries)
        self._last_log_width = log.size.width
        self._last_log_entries = entries

        # Check if scrolled to bottom before clearing
        at_bottom = log.scroll_offset.y >= log.max_scroll_y - 1

        log.clear()
        if not entries:
            log.write(Text("No logs yet", style="dim"))
            return
        for entry in entries:
            log.write(Text(""))
            if entry.kind == LogEntryKind.USER:
                prompt = Text("❯ ", style="bold")
                lines = entry.text.split("\n")
                body = Text(lines[0])
                for line in lines[1:]:
                    body.append("\n  " + line)
                # Resolve theme color for background
                surface = self.current_theme.surface if self.current_theme else "#24283B"
                log.write(
                    Padding(
                        Group(Text.assemble(prompt, body)),
                        (0, 1, 0, 0),
                        style=f"on {surface}",
                        expand=True,
                    ),
                    expand=True,
                )
            elif entry.kind == LogEntryKind.ASSISTANT:
                log.write(Padding(
                    RichMarkdown(entry.text),
                    (0, 1, 0, 2),
                ))
            elif entry.kind == LogEntryKind.THINKING:
                log.write(Padding(
                    Text(entry.text, style="dim italic"),
                    (0, 1, 0, 2),
                ))
            elif entry.kind == LogEntryKind.TOOL_USE:
                from .diff_render import try_render_tool_diff
                is_dark = self.current_theme.dark if self.current_theme else True
                diff_renderable = try_render_tool_diff(entry.name, entry.input, dark=is_dark)
                if diff_renderable:
                    log.write(diff_renderable, expand=True)
                else:
                    warning = self.current_theme.warning if self.current_theme else "#E0AF68"
                    header = Text(f"  ⚡ {entry.name}", style=f"bold {warning}")
                    log.write(header)
                    if entry.input:
                        body = entry.input[:200] + ("..." if len(entry.input) > 200 else "")
                        log.write(Padding(
                            Text(body, style="dim"),
                            (0, 1, 0, 4),
                        ))
            elif entry.kind == LogEntryKind.TOOL_RESULT:
                if entry.content:
                    body = entry.content[:300] + ("..." if len(entry.content) > 300 else "")
                    log.write(Padding(
                        Text(body, style="dim"),
                        (0, 1, 0, 4),
                    ))

        # Only scroll to bottom if we were already there
        if at_bottom:
            log.scroll_end(animate=False)

    def _maybe_refresh_diff(self, force: bool = False) -> None:
        actor = self.query_one(ActorTree).selected_actor
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

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Refresh detail panel when tree selection changes."""
        self._refresh_detail()
        self._maybe_refresh_diff()

    def _focus_detail_content(self) -> None:
        """Focus the first focusable widget inside the active tab pane."""
        try:
            log = self.query_one("#logs-content", RichLog)
            log.focus()
        except Exception:
            pass

    def action_show_tab(self, tab_id: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = tab_id
        if tab_id == "diff":
            self._maybe_refresh_diff(force=True)
        self._focus_detail_content()

    TAB_ORDER = ["logs", "diff", "runs", "info"]

    def action_prev_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        current = tabs.active
        if current in self.TAB_ORDER:
            idx = (self.TAB_ORDER.index(current) - 1) % len(self.TAB_ORDER)
            self.action_show_tab(self.TAB_ORDER[idx])

    def action_next_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        current = tabs.active
        if current in self.TAB_ORDER:
            idx = (self.TAB_ORDER.index(current) + 1) % len(self.TAB_ORDER)
            self.action_show_tab(self.TAB_ORDER[idx])

def run_watch(serve: bool = False) -> None:
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
