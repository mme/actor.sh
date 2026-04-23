"""Main Textual application for actor watch."""

from __future__ import annotations

import threading
from pathlib import Path

from rich.text import Text
from rich.theme import Theme as RichTheme

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Rule,
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
from .omarchy_theme import (
    apply_omarchy_flavor,
    omarchy_theme_mtime,
)
from .themes import CLAUDE_DARK, CLAUDE_LIGHT
from .tree import ActorTree
from .helpers import compute_diff, read_log_entries_since
from .log_renderer import append_log_entries, render_log_entries
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
        /* No horizontal padding on the panel itself so #actors-underline
           can span edge-to-edge and visually continue the tab underline
           from the detail panel. Bottom padding keeps the tree off the
           bottom edge; the ACTORS label and the tree carry their own
           horizontal insets. */
        padding: 0 0 1 0;
        background: ansi_default;
    }
    #actor-panel:focus-within {
        /* intentionally empty for now */
    }
    #actors-label {
        color: $foreground 60%;
        text-style: bold;
        padding: 0 1;
    }
    #actors-underline {
        /* Match the tab underline: heavy horizontal (━), 30%-fg color,
           no margin so it sits flush between label and tree. */
        height: 1;
        margin: 0;
        color: $foreground 30%;
    }
    ActorTree {
        padding-left: 1;
    }
    #app-header {
        display: none;
        background: ansi_default;
    }
    #app-header.-active {
        display: block;
    }
    /* HeaderIcon doubles as the command-palette trigger — we don't
       want either. HeaderClockSpace reserves 10 cols on the right
       even when show_clock is False; hiding both keeps the title
       genuinely centered. */
    #app-header HeaderIcon,
    #app-header HeaderClockSpace,
    #app-header HeaderClock {
        display: none;
    }
    #detail-panel {
        width: 1fr;
        /* Experimental: no border; separator lives on #actor-panel's
           border-right. :focus-within kept as a scaffold (see note
           on #actor-panel). */
    }
    #detail-panel:focus-within {
        /* intentionally empty for now */
    }
    .underline--bar {
        background: $foreground 30%;
    }
    /* Decorate the active tab when anything inside the detail panel
       has focus — that's the "this pane is where your input goes"
       signal. Focus can sit on Tabs itself (arrow-key navigation) OR
       on the tab's content widget (RichLog, etc.) once the user
       drills in, so :focus-within is the right scope. Use the same
       reverse video the focused tree cursor does: $primary bg with
       theme $background text. Arrow prefix is added in Python (see
       _refresh_tab_arrows) because Textual CSS has no ::before. */
    #detail-panel:focus-within Tab.-active {
        background: $primary;
        color: $background;
        text-style: bold;
    }
    * {
        scrollbar-background: $foreground 30%;
        /* Textual's theme defines distinct hover/active track colors
           that default to a much darker band than our idle track — the
           track visibly jumps on mouse-over. Pin both to the idle color
           so only the thumb reacts to hover/drag. */
        scrollbar-background-hover: $foreground 30%;
        scrollbar-background-active: $foreground 30%;
    }
    #logs-content {
        scrollbar-size-horizontal: 0;
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
        Binding("i", "enter_interactive", "Interactive"),
        Binding("l", "show_tab('logs')", "Live"),
        Binding("d", "show_tab('diff')", "Diff"),
        Binding("question_mark", "show_tab('info')", "Info"),
        # Enter is handled by the Tree's NodeSelected message, not an app
        # binding — a priority binding here would steal Enter from the
        # embedded terminal widget.
        Binding("ctrl+shift+d", "dump_diagnostics", show=False),
    ]

    TITLE = "★ actor.sh"

    _prev_statuses: dict[str, Status] = {}
    _current_actors: list[Actor] = []
    _diff_loaded_for: str | None = None

    # Base labels for each tab (without the arrow prefix). Kept separate
    # from the rendered tab.label so we can re-apply the "active +
    # focused → arrow" decoration whenever focus moves or the active
    # tab changes.
    _tab_base_labels: dict[str, str] = {
        "logs": "LIVE",
        "diff": "DIFF",
        "info": "INFO",
        "interactive": "INTERACTIVE",
    }
    _splash_active: bool = False

    # False until on_ready finishes wiring up the initial state. While
    # False, TabActivated messages (fired during initial mount) don't
    # push focus into the detail pane — we want the actor tree to
    # start with focus instead of whatever content widget the default
    # active tab would otherwise claim. Named with the "tabs" prefix
    # because `_ready` is taken by Textual's App internals.
    _tabs_ready: bool = False

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
                yield Static("ACTORS", id="actors-label")
                yield Rule(line_style="heavy", id="actors-underline")
                yield ActorTree()
            with Vertical(id="detail-panel"):
                with TabbedContent(id="tabs"):
                    # Interactive tab is added dynamically via
                    # _sync_detail_view when the selected actor has a
                    # live session; removed again when the session ends.
                    with TabPane("LIVE", id="logs"):
                        yield RichLog(id="logs-content", wrap=True, markup=False, auto_scroll=False)
                    with TabPane("DIFF", id="diff"):
                        yield VerticalScroll(id="diff-scroll")
                    with TabPane("INFO", id="info"):
                        yield VerticalScroll(
                            Static("Select an actor", id="info-content"),
                            DataTable(id="runs-table"),
                        )
        yield Splash(id="splash", animate=self._animate)
        yield Static("Loading...", id="status-bar")
        yield Footer(show_command_palette=False)

    def on_ready(self) -> None:
        self.register_theme(CLAUDE_DARK)
        self.register_theme(CLAUDE_LIGHT)

        # Prefer omarchy's palette when we detect it; fall back to the
        # hardcoded Claude themes otherwise. Live-reload below keeps us
        # in sync with `omarchy theme set <name>`.
        self._omarchy_mtime: float | None = None
        if not self._try_apply_omarchy_theme():
            self.theme = "claude-dark"

        # SIGUSR2 → re-read palette NOW. Used by the optional
        # `~/.config/omarchy/hooks/theme-set` hook installed via
        # `actor setup --for omarchy` to get instant theme swaps (the
        # 3s interval below is the no-setup fallback). Unsupported on
        # Windows / in contexts without a running loop — silent skip;
        # polling still works.
        self._install_sigusr2_handler()

        # Apply Claude Code markdown styles to the app console
        self._apply_markdown_styles()

        for widget in self.query("Tabs, Tab, Footer, DataTable"):
            widget.can_focus = False

        actors, statuses = self._fetch_actors()
        self._update_ui(actors, statuses)
        self.set_interval(2.0, self._poll_actors_async)
        # Omarchy theme live-reload. Polls mtime; cheap enough that a
        # 3s cadence is fine and we don't need inotify / platform-specific
        # machinery. No-op on non-omarchy systems.
        self.set_interval(3.0, self._poll_omarchy_theme)

        # Start with focus on the actor tree. call_after_refresh so
        # the focus call runs AFTER any pending focus-changes queued
        # during compose/mount (TabbedContent's initial tab activation
        # can sneak focus onto a content widget otherwise).
        def _initial_focus() -> None:
            try:
                self.query_one(ActorTree).focus()
            except Exception:
                pass
            self._tabs_ready = True
            self._refresh_tab_arrows()
        self.call_after_refresh(_initial_focus)

    def _install_sigusr2_handler(self) -> None:
        """Wire SIGUSR2 into the asyncio loop so the hook installed via
        `actor setup --for omarchy` can push-notify us on theme change.
        Returns silently when the platform or event-loop state doesn't
        support signal handlers (e.g. Windows, non-main thread)."""
        try:
            import asyncio
            import signal
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGUSR2, self._poll_omarchy_theme)
        except (NotImplementedError, RuntimeError, ValueError):
            # Polling covers us; no need to surface this.
            pass

    def _try_apply_omarchy_theme(self) -> bool:
        """If omarchy is present, flavor the currently active Claude
        base (dark or light) and register the result under its own
        name so no extra picker entry appears. Returns True when
        omarchy affected the rendered theme."""
        return self._reflavor_current_base()

    def _poll_omarchy_theme(self) -> None:
        """Refresh the active theme if omarchy's colors.toml changed.

        Cheap stat call; returns early when the file isn't there or
        its mtime matches what we last saw. Malformed reloads keep
        whatever theme is currently active."""
        current = omarchy_theme_mtime()
        if current is None:
            # File vanished (user uninstalled omarchy or removed the
            # symlink). Leave whatever theme is active as-is; they can
            # re-run `actor watch` to fall back cleanly.
            return
        if self._omarchy_mtime is not None and current == self._omarchy_mtime:
            return
        self._reflavor_current_base()
        self._omarchy_mtime = current

    def _reflavor_current_base(self) -> bool:
        """Flavor whichever CLAUDE_* base matches the currently active
        theme. Keeps user-chosen light/dark preference intact — each
        base carries its own hardcoded surface + foreground, so the
        flavor only shifts the slots apply_omarchy_flavor owns."""
        active = getattr(self, "theme", None)
        if active == "claude-light":
            base = CLAUDE_LIGHT
        else:
            # Default to claude-dark on first application and on any
            # other active name (e.g. textual's built-ins) so we
            # always converge on our dark baseline when omarchy is
            # present.
            base = CLAUDE_DARK
        flavored = apply_omarchy_flavor(base)
        if flavored is None:
            return False
        self._apply_flavored(flavored, base.name)
        self._omarchy_mtime = omarchy_theme_mtime()
        return True

    def _apply_flavored(self, flavored, name: str) -> None:
        """Register the flavored theme and force Textual to re-apply.

        Setting `self.theme = name` when it's already the active name
        is a no-op — the `theme` reactive compares names for equality
        and short-circuits. Calling `_watch_theme` directly runs the
        same invalidation chain the reactive would have run on a real
        name change: toggles the light/dark CSS class, refreshes the
        truecolor filter, and invalidates the compiled stylesheet so
        our new palette actually renders."""
        self.register_theme(flavored)
        if self.theme != name:
            self.theme = name
            return
        # Private but stable: Textual's public API has no "force
        # re-apply without changing the name" hook.
        self._watch_theme(self.theme)

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
        # Overlay INTERACTIVE status for actors with a live terminal
        # session. This is display-only — the DB Run row is still
        # RUNNING/STOPPED/etc.
        for name in self._interactive.live_names():
            if name in statuses:
                statuses[name] = Status.INTERACTIVE

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
        flat_config = {**actor.config.agent_args, **actor.config.actor_keys}
        config_str = "\n".join(f"  {k}={v}" for k, v in sorted(flat_config.items())) if flat_config else "  (none)"
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
        # Cursor-based tail read: only the bytes added since last poll
        # are re-parsed. On actor change the cursor will be None so
        # we get a full read.
        #
        # Serialize under _log_lock so two polls firing in quick
        # succession can't both read from the same pre-update cursor.
        # @work(exclusive=True) only sets a cancel flag the worker
        # must check; synchronous disk I/O won't. The lock both
        # gates the read and scopes the cursor update, so by the
        # time the next worker takes the lock it sees the advanced
        # cursor and skips already-read bytes.
        with self._log_lock:
            cursor = self._log_cursors.get(actor.name)
            new_entries, next_cursor = read_log_entries_since(actor, cursor)
            if next_cursor is not None:
                self._log_cursors[actor.name] = next_cursor
        self.call_from_thread(
            self._append_logs, actor.name, new_entries,
        )

    # Accumulated log entries + read-cursor kept per-actor so switching
    # actors doesn't force a re-parse from byte 0 when the user comes
    # back. Entries survive for the TUI session lifetime.
    _log_entries_by_actor: dict[str, list] = {}
    _log_cursors: dict[str, object] = {}
    _log_lock = threading.Lock()

    _last_log_actor: str | None = None
    _last_log_count: int = 0
    _last_log_width: int = 0
    _last_log_entries: list = []

    def on_resize(self) -> None:
        log = self.query_one("#logs-content", RichLog)
        if log.size.width != self._last_log_width and self._last_log_entries:
            self._last_log_count = 0
            self._set_logs(self._last_log_actor, self._last_log_entries)

    def _append_logs(self, actor_name: str, new_entries: list) -> None:
        """Main-thread callback from _refresh_logs worker. The cursor
        was already advanced by the worker under _log_lock, so here we
        just extend the bucket and run the render. Mutating the bucket
        (shared only with main thread) is safe; the cursor dict is
        worker-written under the lock so we don't touch it here."""
        bucket = self._log_entries_by_actor.setdefault(actor_name, [])
        if new_entries:
            bucket.extend(new_entries)
        self._set_logs(actor_name, bucket)

    def _set_logs(self, actor_name: str, entries: list) -> None:
        log = self.query_one("#logs-content", RichLog)

        # TabbedContent hides non-active panes via display:none, which
        # collapses RichLog to zero width. Writing renderables to a
        # zero-width RichLog still caches line segments — at zero
        # width — so when the user later activates the LIVE tab the
        # first paint is the collapsed cache, visibly shrunken,
        # immediately followed by a re-render at the real width.
        # Stash entries but skip the render while we can't draw the
        # real layout; _flush_pending_logs re-invokes us once LIVE
        # is actually visible.
        if log.size.width == 0:
            self._last_log_actor = actor_name
            self._last_log_entries = entries
            return

        actor_changed = actor_name != self._last_log_actor
        width_changed = log.size.width != self._last_log_width
        if not actor_changed and not width_changed and len(entries) == self._last_log_count:
            return

        prior_count = self._last_log_count
        self._last_log_actor = actor_name
        self._last_log_count = len(entries)
        self._last_log_width = log.size.width
        self._last_log_entries = entries

        at_bottom = log.scroll_offset.y >= log.max_scroll_y - 1

        t = self.current_theme
        # user_fg mirrors Claude Code's behavior: the user-message text
        # uses the BASE theme's foreground (not the active theme's,
        # which may have been flavored by omarchy). Pinning it to the
        # unflavored base keeps legible contrast against the base-defined
        # surface color no matter how much the flavor shifts the rest
        # of the palette.
        base = CLAUDE_DARK if (t and t.dark) else CLAUDE_LIGHT
        colors = ThemeColors(
            surface=t.surface if t else "#24283B",
            warning=t.warning if t else "#E0AF68",
            is_dark=t.dark if t else True,
            success_color=t.success if t else "#4EBA65",
            error_color=t.error if t else "#FF6B80",
            inactive="#999999" if (t and t.dark) else "#666666",
            user_fg=base.foreground,
        )

        # Append vs full rerender. Conditions for the cheap append path:
        #   - same actor (prior_count + entries refer to the same stream)
        #   - same width (RichLog's cached segments are width-specific)
        #   - entry list only grew (prior_count <= new_count)
        #   - the new tail contains no tool_result (if it did, it might
        #     pair with a tool_use we already wrote to the log — RichLog
        #     is append-only, we can't patch the old tool's rendered
        #     row in place, so we fall back to a full rerender)
        can_append = (
            not actor_changed
            and not width_changed
            and 0 <= prior_count <= len(entries)
            and not self._tail_has_tool_result(entries, prior_count)
        )
        if can_append:
            append_log_entries(log, entries, prior_count, colors)
        else:
            render_log_entries(log, entries, colors)

        if at_bottom:
            log.scroll_end(animate=False)

    @staticmethod
    def _tail_has_tool_result(entries: list, prior_count: int) -> bool:
        from ..interfaces import LogEntryKind
        for i in range(prior_count, len(entries)):
            if entries[i].kind == LogEntryKind.TOOL_RESULT:
                return True
        return False

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
        if added or removed:
            base = f"DIFF (±{added + removed})"
        else:
            base = "DIFF"
        self._tab_base_labels["diff"] = base
        self._refresh_tab_arrows()

    def _refresh_tab_arrows(self) -> None:
        """Prefix the currently-active tab with `→` while any
        descendant of the detail panel has focus (tabs bar, the
        active tab's content, etc.). Matches the focus-gated
        reverse-video override in CSS. Safe to call before compose
        finishes."""
        try:
            tabbed = self.query_one("#tabs", TabbedContent)
            detail = self.query_one("#detail-panel")
        except Exception:
            return
        focused = detail.has_focus_within
        active_id = tabbed.active
        for tab_id, base in self._tab_base_labels.items():
            try:
                tab = tabbed.get_tab(tab_id)
            except Exception:
                continue  # e.g. interactive tab isn't mounted right now
            if tab is None:
                continue
            if tab_id == active_id and focused:
                tab.label = f"→ {base}"
            else:
                tab.label = base

    def on_tabbed_content_tab_activated(self, event) -> None:
        # TabbedContent fires TabActivated on mount for the default
        # tab; suppress the focus-push until we're fully ready so the
        # tree (not the default tab's content) carries the initial
        # focus. After ready, push focus into the active tab's
        # content widget so the user can immediately scroll /
        # interact, and so our #detail-panel:focus-within highlight
        # fires on mouse clicks too.
        if self._tabs_ready:
            self._focus_detail_content()
        self._refresh_tab_arrows()
        # LIVE just became visible. Its RichLog was width-0 while
        # hidden and we skipped render passes — flush now so the
        # first paint sees real content at the real width instead
        # of a stale/empty cache that then gets corrected.
        self._flush_pending_logs_if_visible()

    def _flush_pending_logs_if_visible(self) -> None:
        """Re-run the logs renderer once LIVE has a non-zero width,
        using whatever entries were stashed while hidden. Call after
        layout can assign the RichLog its real width (post
        TabActivated + post call_after_refresh is a safe time)."""
        if not self._last_log_entries or self._last_log_actor is None:
            return
        def _attempt() -> None:
            try:
                log = self.query_one("#logs-content", RichLog)
            except Exception:
                return
            if log.size.width == 0:
                return
            # Force a re-render by invalidating the width cache, then
            # replay through _set_logs so the skip-fast-path doesn't
            # short-circuit.
            self._last_log_width = -1
            self._set_logs(self._last_log_actor, self._last_log_entries)
        self.call_after_refresh(_attempt)

    @on(events.Click, "#tabs Tabs, #tabs Tab")
    def _on_tabs_click(self, event: events.Click) -> None:
        # Any click landing inside the Tabs bar — whether on a
        # different tab (triggering TabActivated) or on the
        # already-active tab (which wouldn't) — should end with focus
        # on the active tab's content, matching the keyboard path.
        self._focus_detail_content()

    def on_descendant_focus(self, event) -> None:
        self._refresh_tab_arrows()

    def on_descendant_blur(self, event) -> None:
        self._refresh_tab_arrows()

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
        """Add/remove the Interactive tab based on whether the selected
        actor has a live session. Does NOT activate the tab; use
        action_enter_interactive for that."""
        from textual.css.query import NoMatches

        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except NoMatches:
            return
        actor = self.query_one(ActorTree).selected_actor
        info = (
            self._interactive.get(actor.name) if actor is not None else None
        )

        existing: TabPane | None = None
        try:
            existing = tabs.get_pane("interactive")
        except Exception:
            existing = None

        if info is None:
            if existing is not None:
                tabs.remove_pane("interactive")
            self._interactive_active = None
            return

        # Actor has a live session. Rebuild the Interactive pane so its
        # child widget matches the currently-selected actor's terminal
        # (each actor owns a distinct TerminalWidget).
        if existing is not None:
            # If the existing pane already holds this actor's widget, leave it.
            if info.widget in list(existing.children):
                self._interactive_active = actor.name
                return
            tabs.remove_pane("interactive")

        # add_pane is async — it schedules the mount but we can proceed.
        new_pane = TabPane("INTERACTIVE", info.widget, id="interactive")
        tabs.add_pane(new_pane, before="logs")
        self._interactive_active = actor.name

    def action_enter_interactive(self) -> None:
        """Bound to `i` (app-wide) and Tree.NodeSelected (Enter on tree):
        take the user to the Interactive tab for the selected actor,
        creating the session if needed. Always scrolls to the bottom
        and focuses the terminal so the user is ready to type."""
        actor = self.query_one(ActorTree).selected_actor
        if actor is None:
            self.notify("no actor selected", severity="warning")
            return

        if not self._interactive.has(actor.name):
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
                    config=actor.config,
                )
            except Exception as e:
                self.notify(f"failed to start interactive session: {e}", severity="error")
                return

        self._sync_detail_view()

        # Activate the Interactive tab, focus the terminal, and scroll
        # to the bottom — intentionally different from tab-nav, which
        # preserves scroll position.
        info = self._interactive.get(actor.name)
        if info is None:
            return
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        tabs.active = "interactive"

        target_widget = info.widget

        def _activate() -> None:
            target_widget.focus()
            try:
                target_widget.scroll_end(animate=False, force=True)
            except Exception:
                pass

        self.call_after_refresh(_activate)

    def on_terminal_widget_exit_requested(self, message: TerminalWidget.ExitRequested) -> None:
        """Ctrl+Z from the embedded terminal: move focus to the actor tree.
        The Logs tab keeps the live terminal mounted; Diff / Info tabs
        are still reachable via the tab bar."""
        self.set_focus(None)
        self.call_after_refresh(lambda: self.query_one(ActorTree).focus())

    def on_terminal_widget_session_exited(self, message: TerminalWidget.SessionExited) -> None:
        target: str | None = None
        for name in self._interactive.live_names():
            info = self._interactive.get(name)
            if info is not None and info.widget is message.widget:
                target = name
                break
        if target is None:
            return
        self._interactive.close(target)
        # _sync_detail_view swaps the Logs-tab content back to RichLog
        # now that the session is gone from the registry.
        self._refresh_detail()
        self._sync_detail_view()
        # The terminal widget just unmounted — drop focus onto the tree
        # so the user can immediately navigate to another actor.
        self.set_focus(None)
        self.call_after_refresh(lambda: self.query_one(ActorTree).focus())

    def action_dump_diagnostics(self) -> None:
        import sys
        dump = self._diagnostics.format(limit=200)
        print(f"--- terminal diagnostics ({len(self._diagnostics)} events) ---",
              file=sys.stderr)
        print(dump, file=sys.stderr)
        print("--- end ---", file=sys.stderr)
        self.notify(f"dumped {len(self._diagnostics)} diagnostic events to stderr")

    def on_unmount(self) -> None:
        """Textual app teardown: kill all live interactive subprocesses so
        no PTY child outlives the watch process. Uses the non-blocking
        shutdown path — a blocking waitpid here stalls Textual's own
        shutdown coroutine and leads to a hang that only Ctrl+C escapes."""
        self._interactive.shutdown()

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
