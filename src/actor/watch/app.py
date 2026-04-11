"""Main Textual application for actor watch."""

from __future__ import annotations

from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ..db import Database
from ..process import RealProcessManager
from ..types import Actor, Status
from ..cli import _db_path
from .patches import apply_patches
from .themes import CLAUDE_DARK, CLAUDE_LIGHT
from .tree import ActorTree
from .helpers import read_log_entries, compute_diff
from .log_renderer import render_log_entries
from .types import ThemeColors

# Apply patches at import time
apply_patches()


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
        scrollbar-size: 0 0;
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

        for widget in self.query("Tabs, Tab, Footer, DataTable"):
            widget.can_focus = False

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

        actor_list = self.query_one(ActorTree)
        actor_list.update_actors(actors, statuses)

        running = sum(1 for s in statuses.values() if s == Status.RUNNING)
        done = sum(1 for s in statuses.values() if s == Status.DONE)
        errors = sum(1 for s in statuses.values() if s == Status.ERROR)
        total = len(actors)
        status_bar = self.query_one("#status-bar", Static)
        status_bar.update(
            f" {total} actors: {running} running, {done} done, {errors} error"
            f"{'  ' * 10}localhost:2204"
        )

        self._refresh_detail()

    def _refresh_detail(self) -> None:
        actor_list = self.query_one(ActorTree)
        actor = actor_list.selected_actor
        if actor is None:
            return

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

        self._refresh_runs(actor)
        self._refresh_logs(actor)

    # -- Logs ----------------------------------------------------------------

    @work(thread=True, exclusive=True, group="logs")
    def _refresh_logs(self, actor: Actor) -> None:
        entries = read_log_entries(actor)
        self.call_from_thread(self._set_logs, entries)

    _last_log_count: int = 0
    _last_log_width: int = 0
    _last_log_entries: list = []

    def on_resize(self) -> None:
        log = self.query_one("#logs-content", RichLog)
        if log.size.width != self._last_log_width and self._last_log_entries:
            self._last_log_count = 0
            self._set_logs(self._last_log_entries)

    def _set_logs(self, entries: list) -> None:
        log = self.query_one("#logs-content", RichLog)

        if len(entries) == self._last_log_count and log.size.width == self._last_log_width:
            return
        self._last_log_count = len(entries)
        self._last_log_width = log.size.width
        self._last_log_entries = entries

        at_bottom = log.scroll_offset.y >= log.max_scroll_y - 1

        t = self.current_theme
        colors = ThemeColors(
            surface=t.surface if t else "#24283B",
            warning=t.warning if t else "#E0AF68",
            is_dark=t.dark if t else True,
            success_color=t.success if t else "#4EBA65",
            error_color=t.error if t else "#FF6B80",
            inactive="#999999" if (t and t.dark) else "#666666",
        )
        render_log_entries(log, entries, colors)

        if at_bottom:
            log.scroll_end(animate=False)

    # -- Diff ----------------------------------------------------------------

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
        from .diff_render import render_edit_diff

        diff_data = compute_diff(actor)

        if diff_data is None:
            self.call_from_thread(self._set_diff_text, "No diff available (no worktree or no changes)")
            return

        path_orig, path_mod, orig, mod = diff_data
        if orig == mod:
            self.call_from_thread(self._set_diff_text, "No changes")
            return

        try:
            is_dark = self.current_theme.dark if self.current_theme else True
            renderable = render_edit_diff(path_mod, orig, mod, dark=is_dark)
            self.call_from_thread(self._set_diff_widget, Static(renderable))
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

    # -- Runs ----------------------------------------------------------------

    def _refresh_runs(self, actor: Actor) -> None:
        db = Database.open(_db_path())
        runs, _total = db.list_runs(actor.name, limit=50)
        table = self.query_one("#runs-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Status", "Exit", "Prompt", "Started", "Duration")
        for run in reversed(runs):
            duration = ""
            if run.started_at and run.finished_at:
                from ..types import _parse_iso
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

    def on_tree_node_highlighted(self, event) -> None:
        self._refresh_detail()
        self._maybe_refresh_diff()

    def _focus_detail_content(self, tab_id: str | None = None) -> None:
        if tab_id is None:
            tabs = self.query_one("#tabs", TabbedContent)
            tab_id = tabs.active

        focus_map = {
            "logs": "#logs-content",
            "diff": "#diff-scroll",
            "runs": "#runs-table",
            "info": "#info-content",
        }
        selector = focus_map.get(tab_id)
        if selector:
            try:
                widget = self.query_one(selector)
                widget.can_focus = True
                widget.focus()
            except Exception:
                pass

    def action_show_tab(self, tab_id: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = tab_id
        if tab_id == "diff":
            self._maybe_refresh_diff(force=True)
        self._focus_detail_content(tab_id)

    TAB_ORDER = ["logs", "diff", "runs", "info"]

    def action_prev_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        current = tabs.active
        if current in self.TAB_ORDER:
            idx = self.TAB_ORDER.index(current)
            if idx == 0:
                self.query_one(ActorTree).focus()
            else:
                self.action_show_tab(self.TAB_ORDER[idx - 1])

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
