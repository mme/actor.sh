"""Main Textual application for actor watch."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from rich.theme import Theme as RichTheme

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    Footer,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ..db import Database
from ..interfaces import binary_exists
from ..process import RealProcessManager
from ..types import Actor, AgentKind, Status
from ..cli import _db_path, _create_agent
from .interactive.diagnostics import DiagnosticRecorder
from .interactive.manager import InteractiveSessionManager
from .interactive.widget import TerminalWidget
from .patches import apply_patches
from .splash import Splash
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
    #actor-panel {
        width: 33;
        border: blank;
        padding: 0 1;
        background: ansi_default;
    }
    #actor-panel:focus-within {
        border: round $primary;
    }
    #actor-title {
        text-style: bold;
        color: $secondary;
        height: 1;
        margin-bottom: 1;
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
    SearchIcon {
        color: $text;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #splash, #main-layout {
        display: none;
    }
    #splash.-active, #main-layout.-active {
        display: block;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("left,ctrl+b", "navigate_left", show=False),
        Binding("right,ctrl+f", "navigate_right", show=False),
        Binding("up,ctrl+p", "navigate_up", show=False),
        Binding("down,ctrl+n", "navigate_down", show=False),
        Binding("a", "focus_actors", "Actors"),
        Binding("p", "command_palette", "Palette"),
        Binding("l", "show_tab('logs')", "Logs"),
        Binding("d", "show_tab('diff')", "Diff"),
        Binding("i", "show_tab('info')", "Info"),
        Binding("enter", "enter_interactive", "Interactive", show=True),
        Binding("ctrl+shift+d", "dump_diagnostics", show=False),
    ]

    _prev_statuses: dict[str, Status] = {}
    _current_actors: list[Actor] = []
    _diff_loaded_for: str | None = None
    _splash_active: bool = False

    def __init__(self, animate: bool = True) -> None:
        super().__init__()
        self._animate = animate
        self._diagnostics = DiagnosticRecorder(capacity=2048)
        self._interactive = InteractiveSessionManager(
            db_opener=lambda: Database.open(_db_path()),
            recorder=self._diagnostics,
        )
        # Actor whose terminal widget is currently mounted, if any.
        self._interactive_active: str | None = None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self._splash_active and action != "quit":
            return False
        return True

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-layout"):
            with Vertical(id="actor-panel"):
                yield Static(" ★ ACTOR.SH", id="actor-title")
                yield ActorTree()
            with Vertical(id="detail-panel"):
                with ContentSwitcher(id="detail-switcher", initial="tabs-view"):
                    with Vertical(id="tabs-view"):
                        with TabbedContent(id="tabs"):
                            with TabPane("Logs", id="logs"):
                                yield RichLog(id="logs-content", wrap=True, markup=False, auto_scroll=False)
                            with TabPane("Diff", id="diff"):
                                yield VerticalScroll(id="diff-scroll")
                            with TabPane("Info", id="info"):
                                yield VerticalScroll(
                                    Static("Select an actor", id="info-content"),
                                    DataTable(id="runs-table"),
                                )
                    yield Vertical(id="interactive-view")
        yield Splash(id="splash", animate=self._animate)
        yield Static("Loading...", id="status-bar")
        yield Footer(show_command_palette=False)

    def on_ready(self) -> None:
        self.register_theme(CLAUDE_DARK)
        self.register_theme(CLAUDE_LIGHT)
        self.theme = "claude-dark"

        # Apply Claude Code markdown styles to the app console
        self._apply_markdown_styles()

        for widget in self.query("Tabs, Tab, Footer, DataTable"):
            widget.can_focus = False

        actors, statuses = self._fetch_actors()
        self._update_ui(actors, statuses)
        self.set_interval(2.0, self._poll_actors_async)

    def _apply_markdown_styles(self) -> None:
        """Override Rich console markdown styles to match Claude Code."""
        is_dark = self.current_theme.dark if self.current_theme else True
        code_color = "#B1B9F9" if is_dark else "#5769F7"
        self.console.push_theme(RichTheme({
            "markdown.code": code_color,
            "markdown.code_block": "none",
            "markdown.h1": "bold italic underline",
            "markdown.h1.border": "none",
            "markdown.h2": "bold",
            "markdown.h3": "bold",
            "markdown.h4": "bold",
            "markdown.h5": "bold",
            "markdown.h6": "dim",
            "markdown.em": "italic",
            "markdown.strong": "bold",
            "markdown.link": "blue",
            "markdown.link_url": "blue",
            "markdown.block_quote": "dim italic",
            "markdown.hr": "dim",
            "markdown.item.bullet": "bold",
            "markdown.item.number": "bold",
            "markdown.list": "none",
            "markdown.paragraph": "none",
            "markdown.text": "none",
            "markdown.s": "strike",
        }))

    @staticmethod
    def _fetch_actors() -> tuple[list[Actor], dict[str, Status]]:
        with Database.open(_db_path()) as db:
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

        main = self.query_one("#main-layout")
        splash = self.query_one("#splash")
        was_splash = self._splash_active
        self._splash_active = not actors
        if self._splash_active:
            main.set_class(False, "-active")
            splash.set_class(True, "-active")
        else:
            main.set_class(True, "-active")
            splash.set_class(False, "-active")
        if was_splash != self._splash_active:
            self.refresh_bindings()

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
            self._clear_detail()
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

    def _clear_detail(self) -> None:
        info = self.query_one("#info-content", Static)
        info.update("Select an actor")

        table = self.query_one("#runs-table", DataTable)
        table.clear(columns=True)

        log = self.query_one("#logs-content", RichLog)
        log.clear()
        self._last_log_actor = None
        self._last_log_count = 0
        self._last_log_entries = []

        scroll = self.query_one("#diff-scroll", VerticalScroll)
        scroll.remove_children()
        self._diff_loaded_for = None
        self._update_diff_tab_label()

    # -- Logs ----------------------------------------------------------------

    @work(thread=True, exclusive=True, group="logs")
    def _refresh_logs(self, actor: Actor) -> None:
        entries = read_log_entries(actor)
        self.call_from_thread(self._set_logs, actor.name, entries)

    _last_log_actor: str | None = None
    _last_log_count: int = 0
    _last_log_width: int = 0
    _last_log_entries: list = []

    def on_resize(self) -> None:
        log = self.query_one("#logs-content", RichLog)
        if log.size.width != self._last_log_width and self._last_log_entries:
            self._last_log_count = 0
            self._set_logs(self._last_log_entries)

    def _set_logs(self, actor_name: str, entries: list) -> None:
        log = self.query_one("#logs-content", RichLog)

        actor_changed = actor_name != self._last_log_actor
        if not actor_changed and len(entries) == self._last_log_count and log.size.width == self._last_log_width:
            return
        self._last_log_actor = actor_name
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

        result = compute_diff(actor)

        if result.files is None:
            self.call_from_thread(self._set_diff_text, result.reason)
            return

        try:
            is_dark = self.current_theme.dark if self.current_theme else True
            from rich.console import Group
            import difflib
            parts = []
            total_added = 0
            total_removed = 0
            from rich.text import Text as RichText
            for fd in result.files:
                parts.append(render_edit_diff(fd.file_path, fd.old_content, fd.new_content, dark=is_dark, style="diff"))
                parts.append(RichText(""))
                for line in difflib.unified_diff(fd.old_content.splitlines(), fd.new_content.splitlines(), lineterm=""):
                    if line.startswith("+") and not line.startswith("+++"):
                        total_added += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        total_removed += 1
            self.call_from_thread(self._set_diff_widget, Static(Group(*parts)), total_added, total_removed)
        except Exception as e:
            self.call_from_thread(self._set_diff_text, f"Diff error: {e}")

    def _update_diff_tab_label(self, added: int = 0, removed: int = 0) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tab = tabs.get_tab("diff")
        if added or removed:
            tab.label = f"Diff (±{added + removed})"
        else:
            tab.label = "Diff"

    def _set_diff_text(self, text: str) -> None:
        scroll = self.query_one("#diff-scroll", VerticalScroll)
        scroll.remove_children()
        scroll.mount(Static(text))
        self._update_diff_tab_label()

    def _set_diff_widget(self, dv: object, added: int = 0, removed: int = 0) -> None:
        scroll = self.query_one("#diff-scroll", VerticalScroll)
        scroll.remove_children()
        scroll.mount(dv)
        self._update_diff_tab_label(added, removed)

    # -- Runs ----------------------------------------------------------------

    def _refresh_runs(self, actor: Actor) -> None:
        with Database.open(_db_path()) as db:
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
        self._sync_detail_view()

    # -- Interactive mode ----------------------------------------------------

    def _sync_detail_view(self) -> None:
        """Switch the detail panel between tabs and an interactive terminal
        based on whether the currently-selected actor has a live session."""
        try:
            switcher = self.query_one("#detail-switcher", ContentSwitcher)
        except Exception:
            return
        actor = self.query_one(ActorTree).selected_actor
        if actor is None or not self._interactive.has(actor.name):
            if switcher.current != "tabs-view":
                switcher.current = "tabs-view"
                self._interactive_active = None
            return

        info = self._interactive.get(actor.name)
        if info is None:
            switcher.current = "tabs-view"
            self._interactive_active = None
            return

        # Unmount any previously-shown terminal, mount this one.
        view = self.query_one("#interactive-view", Vertical)
        for child in list(view.children):
            child.remove()
        view.mount(info.widget)
        switcher.current = "interactive-view"
        self._interactive_active = actor.name
        info.widget.focus()

    def action_enter_interactive(self) -> None:
        """Enter key on the tree: start an interactive session for the
        selected actor (or show its existing one)."""
        # Only trigger if the tree has focus — otherwise leave Enter alone
        # for the other widgets that may consume it.
        if not self._tree_has_focus():
            return

        actor = self.query_one(ActorTree).selected_actor
        if actor is None:
            return

        if self._interactive.has(actor.name):
            self._sync_detail_view()
            return

        status = self._prev_statuses.get(actor.name, Status.IDLE)
        if status == Status.RUNNING:
            self.notify(f"{actor.name} is currently running — stop it first",
                        severity="error")
            return
        if actor.agent_session is None:
            self.notify(f"{actor.name} has no session yet — run it first",
                        severity="error")
            return
        if not binary_exists(actor.agent.binary_name):
            self.notify(f"agent binary '{actor.agent.binary_name}' not on PATH",
                        severity="error")
            return

        try:
            self._interactive.create(
                actor_name=actor.name,
                agent=_create_agent(actor.agent),
                session_id=actor.agent_session,
                cwd=Path(actor.dir),
                config=dict(actor.config),
            )
        except Exception as e:
            self.notify(f"failed to start interactive session: {e}", severity="error")
            return

        self._sync_detail_view()

    def on_terminal_widget_exit_requested(self, message: TerminalWidget.ExitRequested) -> None:
        """Ctrl+Z: leave interactive mode but keep the session alive in the bg."""
        # Find the actor this widget belongs to.
        name = self._interactive_active
        if name is None:
            return
        try:
            switcher = self.query_one("#detail-switcher", ContentSwitcher)
            switcher.current = "tabs-view"
        except Exception:
            pass
        # Return focus to the tree.
        self.query_one(ActorTree).focus()
        self._interactive_active = None

    def on_terminal_widget_session_exited(self, message: TerminalWidget.SessionExited) -> None:
        """Child process exited on its own. Close the session and restore logs."""
        # Find the matching actor in the manager.
        target: str | None = None
        for name in self._interactive.live_names():
            info = self._interactive.get(name)
            if info is not None and info.widget is message.widget:
                target = name
                break
        if target is None:
            return
        self._interactive.close(target)
        if self._interactive_active == target:
            try:
                switcher = self.query_one("#detail-switcher", ContentSwitcher)
                switcher.current = "tabs-view"
            except Exception:
                pass
            self._interactive_active = None
        code = message.exit_code
        severity = "information" if code == 0 else "warning"
        self.notify(f"{target} interactive session ended (exit {code})", severity=severity)
        # Refresh the logs so the new *interactive* run row is visible.
        self._refresh_detail()

    def action_dump_diagnostics(self) -> None:
        """Dump the terminal-I/O diagnostics ring buffer to the notification
        system and stderr. Hidden binding for flicker post-mortems."""
        import sys
        dump = self._diagnostics.format(limit=200)
        print(f"--- terminal diagnostics ({len(self._diagnostics)} events) ---",
              file=sys.stderr)
        print(dump, file=sys.stderr)
        print("--- end ---", file=sys.stderr)
        self.notify(f"dumped {len(self._diagnostics)} diagnostic events to stderr")

    def on_unmount(self) -> None:
        """Textual app teardown: kill all live interactive subprocesses so
        no PTY child outlives the watch process."""
        try:
            self._interactive.close_all()
        except Exception:
            pass

    def _focus_detail_content(self, tab_id: str | None = None) -> None:
        if tab_id is None:
            tabs = self.query_one("#tabs", TabbedContent)
            tab_id = tabs.active

        focus_map = {
            "logs": "#logs-content",
            "diff": "#diff-scroll",
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

    TAB_ORDER = ["logs", "diff", "info"]

    def action_focus_actors(self) -> None:
        self.query_one(ActorTree).focus()

    def _tree_has_focus(self) -> bool:
        return self.query_one(ActorTree).has_focus

    def action_navigate_left(self) -> None:
        if self._tree_has_focus():
            tree = self.query_one(ActorTree)
            node = tree.cursor_node
            if node and node.children and node.is_expanded:
                node.collapse()
            return
        tabs = self.query_one("#tabs", TabbedContent)
        current = tabs.active
        if current in self.TAB_ORDER:
            idx = self.TAB_ORDER.index(current)
            if idx == 0:
                self.query_one(ActorTree).focus()
            else:
                self.action_show_tab(self.TAB_ORDER[idx - 1])

    def action_navigate_right(self) -> None:
        if self._tree_has_focus():
            tree = self.query_one(ActorTree)
            node = tree.cursor_node
            if node and node.children and not node.is_expanded:
                node.expand()
            else:
                self._focus_detail_content()
        else:
            tabs = self.query_one("#tabs", TabbedContent)
            current = tabs.active
            if current in self.TAB_ORDER:
                idx = self.TAB_ORDER.index(current)
                if idx < len(self.TAB_ORDER) - 1:
                    self.action_show_tab(self.TAB_ORDER[idx + 1])

    def action_navigate_up(self) -> None:
        if self._tree_has_focus():
            self.query_one(ActorTree).action_cursor_up()
        else:
            # Scroll up in the detail view
            focused = self.focused
            if focused and hasattr(focused, 'scroll_up'):
                focused.scroll_up()

    def action_navigate_down(self) -> None:
        if self._tree_has_focus():
            self.query_one(ActorTree).action_cursor_down()
        else:
            focused = self.focused
            if focused and hasattr(focused, 'scroll_down'):
                focused.scroll_down()


def run_watch(serve: bool = False, animate: bool = True) -> None:
    """Entry point for `actor watch`."""
    if serve:
        try:
            from textual_serve.server import Server
            cmd = "uv run python -m actor.watch --no-serve"
            if not animate:
                cmd += " --no-animation"
            server = Server(cmd, port=2204)
            server.serve()
        except ImportError:
            app = ActorWatchApp(animate=animate)
            app.run()
    else:
        app = ActorWatchApp(animate=animate)
        app.run()


def main() -> None:
    """Direct entry point when run as module."""
    import sys
    serve = "--no-serve" not in sys.argv
    animate = "--no-animation" not in sys.argv
    run_watch(serve=serve, animate=animate)
