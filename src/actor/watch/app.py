"""Main Textual application for actor watch."""

from __future__ import annotations

import threading
from pathlib import Path

from rich.text import Text
from rich.theme import Theme as RichTheme

from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
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
from ..interfaces import LogEntryKind, binary_exists
from ..process import RealProcessManager
from ..types import Actor, AgentKind, Status
from ..cli import _db_path, _create_agent
from .confirm_dialog import ConfirmDialog
from .help_overlay import HelpOverlay
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
from .diff_render import iter_diff_renderables
from .prerendered_diff import PrerenderedDiff, renderable_to_strips
from .helpers import (
    compute_diff,
    compute_diff_shortstat,
    git_index_mtime,
    git_untracked_count,
    read_head_oid,
    read_log_entries_since,
)
from .log_renderer import (
    append_log_entries,
    apply_log_renderables,
    build_log_renderables,
)
from .types import ThemeColors

# Apply patches at import time
apply_patches()


class ActorWatchApp(App):
    """Real-time dashboard for actor.sh."""

    CSS = """
    Screen, Tabs, Tab, TabbedContent, TabPane,
    ContentSwitcher, VerticalScroll, RichLog,
    DataTable, Tree, #detail-panel {
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
        text-style: none;
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
    #detail-panel:focus-within Tab.-active,
    #detail-panel.-focus-active Tab.-active {
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
        /* Lives inside `#overview-scroll`, below the auto-height
           header card. `1fr` claims the remaining vertical space so
           the log feed fills from the bottom of the header to the
           bottom of the OVERVIEW pane. */
        height: 1fr;
        scrollbar-size-horizontal: 0;
        /* Hide the track — only the thumb remains visible as a
           floating indicator. `ansi_default` makes the track blend
           into whatever the terminal's background is, overriding
           the 30%-fg track color we apply globally via `*`. */
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
    }
    /* OVERVIEW pane is a vertical stack of section widgets. Each
       section paints its own Rich content (with headers / borders /
       layout via Rich primitives), so the widget-level CSS just
       handles spacing and the runs-table which Textual styles
       directly. */
    #overview-scroll {
        padding: 1 0;
    }
    #overview-header {
        height: auto;
        width: 1fr;
        margin-bottom: 1;
    }
    #overview-last-interaction {
        height: auto;
        margin-bottom: 1;
    }
    #overview-runs-label {
        height: 1;
        color: $text-muted;
        text-style: bold;
        margin-bottom: 0;
    }
    #runs-table {
        height: auto;
        width: 1fr;
    }
    /* Experiment: hide LAST INTERACTION + runs sections so the
       OVERVIEW pane is just the header card. Remove these three
       `display: none` rules to bring everything back. */
    #overview-last-interaction { display: none; }
    #overview-runs-label { display: none; }
    #runs-table { display: none; }
    SearchIcon {
        color: $text;
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
        # Arrow + emacs aliases for navigation. `show=False` keeps the
        # Footer uncluttered (a row of arrow keys is noise next to the
        # named actions), but `description` is set so the keys panel
        # surfaces them under the global keymap.
        #
        # priority=True on left/right is load-bearing for #24: every
        # ScrollView descendant (RichLog, VerticalScroll, etc.) inherits
        # "left" / "right" bindings for horizontal scroll. Without
        # priority, the focused widget consumes the arrow before our
        # App-level navigation runs, trapping focus inside the detail
        # pane. `check_action` excludes the embedded TerminalWidget so
        # its passthrough still works; modal screens are excluded too.
        Binding("left,ctrl+b", "navigate_left", "Move left", show=False, priority=True),
        Binding("right,ctrl+f", "navigate_right", "Move right", show=False, priority=True),
        Binding("up,ctrl+p", "navigate_up", "Move up", show=False),
        Binding("down,ctrl+n", "navigate_down", "Move down", show=False),
        Binding("a", "focus_actors", "Actors"),
        Binding("p", "command_palette", "Palette"),
        Binding("i", "enter_interactive", "Interactive"),
        Binding("d", "show_tab('diff')", "Diff"),
        Binding("o", "show_tab('info')", "Overview"),
        # Enter is handled by focused widgets: ActorTree posts
        # NodeSelected for actor activation, and the app-level on_key
        # below handles the interactive tab bar. A priority binding
        # here would steal Enter from the embedded terminal widget.
        Binding("ctrl+shift+d", "dump_diagnostics", show=False),
    ]

    TITLE = "★ actor.sh"

    _prev_statuses: dict[str, Status] = {}
    _current_actors: list[Actor] = []
    _diff_loaded_for: str | None = None

    # Two-phase diff build state — mirrors the logs pattern. The kick
    # path bumps `_diff_build_token` and starts a worker; the worker
    # checks the token cooperatively and only commits its result when
    # the token still matches at apply time. `_diff_build_pending`
    # gates the 300ms placeholder. `_diff_last_applied_key` stores the
    # `(actor.name, head_oid, content_width)` cache key from the most
    # recent successful apply — kicks early-out when the request key
    # matches. `_diff_pending_actor` stashes the actor whose diff was
    # requested while the DIFF tab was hidden (width 0); the next tab
    # activation flushes it.
    _diff_build_token: int = 0
    _diff_build_pending: bool = False
    _diff_build_target_actor: str | None = None
    _diff_build_target_width: int = 0
    _diff_last_applied_key: tuple | None = None
    _diff_pending_actor: str | None = None
    # Stage-4 streaming: which token's first-file append landed on
    # `#diff-scroll`. The first append for a given kick clears the
    # scroll (placeholder + any prior content); subsequent appends
    # for the same token just mount. -1 is a sentinel that never
    # matches a live token.
    _diff_streamed_token: int = -1

    # Cheap-badge-first state. Independent of the full build path —
    # the badge worker runs `git diff --shortstat` to produce a
    # near-instant ±N for the tab label while the heavier full build
    # is still parsing diffs and rendering Tables. Token discipline
    # mirrors the build path but uses its own counter so the two
    # paths cancel independently.
    _diff_badge_token: int = 0
    _diff_badge_target_actor: str | None = None

    # Per-actor (added, removed) line counts, written by both the
    # cheap badge worker and the authoritative build worker. Single
    # source of truth for the DIFF tab label and the OVERVIEW
    # branch row's `+N -M` segment — both surfaces read from here so
    # they can never drift. Missing entry → no counts known yet (the
    # background poller fills it in within ~100ms of selection).
    _diff_counts_by_actor: dict[str, tuple[int, int]] = {}

    # Live-refresh poll state. While DIFF is active and the selected
    # actor is RUNNING, a 2s interval ticks
    # `_poll_diff_for_running`, which captures index mtime + shortstat
    # and force-refreshes the diff if either flipped vs the last
    # observation. The first observation after conditions become
    # true is treated as the baseline (no refresh — the user just
    # got here, the build is presumably already correct); subsequent
    # ticks compare. When conditions stop holding, the baseline
    # resets so a later re-entry baselines cleanly.
    _diff_poll_initialized: bool = False
    _diff_poll_last_actor: str | None = None
    _diff_poll_last_index_mtime: float | None = None
    _diff_poll_last_shortstat: tuple[int, int] | None = None
    _diff_poll_last_untracked: int | None = None

    # Base labels for each tab (without the arrow prefix). Kept separate
    # from the rendered tab.label so we can re-apply the "active +
    # focused → arrow" decoration whenever focus moves or the active
    # tab changes.
    # `diff` may hold a Rich Text (with green +N / red -M segments) when
    # there are changes; the others stay strings. `_refresh_tab_arrows`
    # handles either shape when prepending the focus arrow.
    _tab_base_labels: dict[str, str | Text] = {
        "diff": "DIFF",
        "info": "OVERVIEW",
        # `interactive` is recomputed every paint by
        # `_refresh_tab_arrows` so it reflects the live focus state
        # ("[CTRL+Z TO EXIT]" / plain). The
        # placeholder here just keeps the dict shape consistent.
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
        self._pending_interactive_focus = None
        self._interactive_pane_token = 0
        self._preferred_detail_tab = "info"
        self._skip_next_detail_preference_update = False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        nav_actions = {
            "navigate_left",
            "navigate_right",
            "navigate_up",
            "navigate_down",
        }
        if action in nav_actions:
            # Priority bindings bypass `_modal_binding_chain`, so the
            # App-level navigate_* actions fire even when a system
            # modal is on top — the modal user would press an arrow
            # to move focus inside the dialog and instead see the
            # underlying tabs cycle. Suppress navigation while any
            # modal is the active screen.
            from textual.screen import SystemModalScreen
            if isinstance(self.screen, SystemModalScreen):
                return False
            if isinstance(self.focused, TerminalWidget):
                return False
        if self._splash_active and action != "quit":
            return False
        return True

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-layout"):
            with Vertical(id="actor-panel"):
                yield Static("ACTOR.SH", id="actors-label")
                yield Rule(line_style="heavy", id="actors-underline")
                yield ActorTree()
            with Vertical(id="detail-panel"):
                with TabbedContent(id="tabs"):
                    # OVERVIEW is the actor's home page: header, last
                    # interaction, activity stats, recent runs. First
                    # in the tab order so it's the default landing
                    # screen on actor selection. Interactive tab is
                    # added dynamically via _sync_detail_view when
                    # the selected actor has a live session; removed
                    # again when the session ends.
                    with TabPane("OVERVIEW", id="info"):
                        # OVERVIEW pane is a non-scrolling stack: the
                        # header card (auto height) on top, then the
                        # log RichLog filling the remaining vertical
                        # space (1fr). The RichLog has its own
                        # internal scroll, so the log feed scrolls
                        # smoothly under a fixed header. Hidden bits
                        # (last-interaction / runs-table) stay in the
                        # tree so the existing `display: none` rules
                        # and re-render paths keep working unchanged.
                        yield Vertical(
                            Static("", id="overview-header"),
                            Static("", id="overview-last-interaction"),
                            Static("Runs", id="overview-runs-label"),
                            Static("", id="runs-table"),
                            RichLog(
                                id="logs-content",
                                wrap=True,
                                markup=False,
                                auto_scroll=False,
                            ),
                            id="overview-scroll",
                        )
                    with TabPane("DIFF", id="diff"):
                        yield VerticalScroll(id="diff-scroll")
        yield Splash(id="splash", animate=self._animate)
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

        for widget in self.query("Tab, Footer, DataTable"):
            widget.can_focus = False
        for widget in self.query("Tabs"):
            widget.can_focus = True

        actors, statuses = self._fetch_actors()
        self._update_ui(actors, statuses)
        self.set_interval(2.0, self._poll_actors_async)
        # Omarchy theme live-reload. Polls mtime; cheap enough that a
        # 3s cadence is fine and we don't need inotify / platform-specific
        # machinery. No-op on non-omarchy systems.
        self.set_interval(3.0, self._poll_omarchy_theme)
        # Live DIFF refresh while the selected actor is RUNNING and
        # DIFF is active. Same 2s cadence as the actor poller; the
        # handler early-outs cheaply when conditions don't hold.
        self.set_interval(2.0, self._poll_diff_for_running)
        # Always-on cheap badge refresh — keeps the DIFF tab label
        # and OVERVIEW `+N -M` honest regardless of tab/status. Two
        # subprocs per tick on a worker thread; cheap enough that the
        # handler doesn't even gate on selection changes.
        self.set_interval(2.0, self._poll_diff_badge_for_selected)
        # 1Hz tick to keep the OVERVIEW header's "running for Xs"
        # counter live. No-ops when no actor is selected; otherwise
        # cheap (one DB read for the most recent run + a Static
        # update). Doesn't compete with the diff poll since they
        # touch disjoint widgets.
        self.set_interval(1.0, self._tick_overview_age)
        # 0.5Hz tick — same cadence as the tree's running animation —
        # so the OVERVIEW header icon advances frame-by-frame in sync
        # with the actor list. Only re-renders when the selected actor
        # is RUNNING (cheap predicate, header-only update).
        self.set_interval(0.5, self._tick_overview_running_icon)
        self.watch(self.screen, "focused", self._on_focused_changed)

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
        # Worker callback. The app may already be tearing down by the
        # time this lands (the test harness exits faster than the
        # periodic poll cancels). When the DOM is gone, bail rather
        # than letting NoMatches propagate out of a Worker — that
        # surfaces as a hard failure in `run_test` even though it's
        # just a benign teardown race.
        from textual.css.query import NoMatches
        try:
            self._update_ui_unsafe(actors, statuses)
        except NoMatches:
            return

    def _update_ui_unsafe(
        self, actors: list[Actor], statuses: dict[str, Status]
    ) -> None:
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
            # Defer so the class-toggle above has settled through the
            # DOM first; otherwise the Footer re-queries bindings from
            # a stale layout and keeps showing the full set.
            self.call_after_refresh(self._refresh_footer_bindings)

        self._refresh_detail()

    def _refresh_footer_bindings(self) -> None:
        self.refresh_bindings()
        # Belt-and-suspenders: force the Footer to recompose so
        # check_action is re-evaluated for every binding.
        try:
            footer = self.query_one(Footer)
            footer.refresh(recompose=True)
        except Exception:
            pass

    def _refresh_detail(self) -> None:
        try:
            actor_list = self.query_one(ActorTree)
        except Exception:
            return
        actor = actor_list.selected_actor
        if actor is None:
            self._clear_detail()
            return

        status = self._prev_statuses.get(actor.name, Status.IDLE)
        # Drop cached repo info when the session_id moved — same
        # discard+recreate-with-same-name discipline the log cache uses.
        last_session = self._repo_info_session_for.get(actor.name)
        if last_session != (actor.agent_session or ""):
            self._repo_info_by_actor.pop(actor.name, None)
            # Cached counts came from the prior incarnation's worktree;
            # drop them so the OVERVIEW branch row + DIFF tab label both
            # blank out until the badge poller fills the new ones in.
            self._diff_counts_by_actor.pop(actor.name, None)
        self._render_overview_header(actor, status)
        self._render_last_interaction(actor)
        self._refresh_runs(actor)
        self._refresh_logs(actor)

    def _clear_detail(self) -> None:
        # Reset all overview sections to empty/placeholder. A future
        # selection re-populates each via `_refresh_detail`.
        for selector in (
            "#overview-header",
            "#overview-last-interaction",
        ):
            try:
                self.query_one(selector, Static).update("")
            except Exception:
                pass
        try:
            label = self.query_one("#overview-runs-label", Static)
            label.update("Select an actor")
        except Exception:
            pass

        try:
            self.query_one("#runs-table", Static).update("")
        except Exception:
            pass

        log = self.query_one("#logs-content", RichLog)
        log.clear()
        self._last_log_actor = None
        self._last_log_count = 0
        self._last_log_entries = []

        scroll = self.query_one("#diff-scroll", VerticalScroll)
        scroll.remove_children()
        self._diff_loaded_for = None
        # Cancel any in-flight diff build / badge worker by bumping
        # both tokens; their applies will see a mismatch and drop
        # their output.
        self._diff_build_token += 1
        self._diff_badge_token += 1
        self._diff_build_pending = False
        self._diff_last_applied_key = None
        self._diff_pending_actor = None
        self._update_diff_tab_label()

    # -- Overview ------------------------------------------------------------

    # The most recently rendered (actor.name, status) for the header,
    # so the 1s age tick can re-render in place without re-querying
    # the tree / DB. None means no actor is selected and the tick
    # should no-op.
    _overview_header_actor: Actor | None = None

    # Three-row LCD-style shapes for the actor monogram in the header
    # icon panel. Each entry is a 3-tuple of strings (top / middle /
    # bottom), each padded to 3 cells wide. We extend Textual's
    # `DigitsRenderable` font with the rest of the alphabet so any
    # actor name's first letter renders in the same seven-segment
    # aesthetic. Letters A-F duplicate Textual's built-ins so we can
    # treat the dict as the single source of truth.
    _LCD_LETTERS: dict[str, tuple[str, str, str]] = {
        "A": ("╭─╮", "├─┤", "╵ ╵"),
        "B": ("┌─╮", "├─┤", "└─╯"),
        "C": ("╭─╮", "│  ", "╰─╯"),
        "D": ("┌─╮", "│ │", "└─╯"),
        "E": ("╭─╴", "├─ ", "╰─╴"),
        "F": ("╭─╴", "├─ ", "╵  "),
        "G": ("╭─╴", "│╶╮", "╰─╯"),
        "H": ("╷ ╷", "├─┤", "╵ ╵"),
        "I": ("╶┬╴", " │ ", "╶┴╴"),
        "J": ("╶─╮", "  │", "╰─╯"),
        "K": ("╷ ╱", "├─╮", "╵ ╵"),
        "L": ("╷  ", "│  ", "╰─╴"),
        "M": ("╭╮╭╮", "│╰╯│", "╵  ╵"),
        "N": ("╭╮╷", "│││", "╵╰╯"),
        "O": ("╭─╮", "│ │", "╰─╯"),
        "P": ("┌─╮", "├─╯", "╵  "),
        "Q": ("╭─╮", "│ │", "╰─╲"),
        "R": ("┌─╮", "├─┤", "╵ ╵"),
        "S": ("╭─╴", "╰─╮", "╶─╯"),
        "T": ("╶┬╴", " │ ", " ╵ "),
        "U": ("╷ ╷", "│ │", "╰─╯"),
        "V": ("   ", "│ │", "╰─╯"),
        "W": ("╷  ╷", "│╭╮│", "╰╯╰╯"),
        "X": ("╲ ╱", " ╳ ", "╱ ╲"),
        "Y": ("╷ ╷", "╰┬╯", " ╵ "),
        "Z": ("╶─╮", "╭─╯", "╰─╴"),
    }

    def _render_overview_header(
        self, actor: Actor, status: Status,
    ) -> None:
        """Build and mount the OVERVIEW pane header — actor name, status
        pill, agent + model + auth, and an age line. The age portion
        is rebuilt every second by `_tick_overview_age`; we stash the
        actor + status here so that tick can run without going back
        through the full _refresh_detail path."""
        from rich import box as rich_box
        from rich.console import Group
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.table import Table
        from rich.text import Text

        self._overview_header_actor = actor

        colors = self._overview_palette()
        title_color, title_label = self._status_pill(status, colors)
        info = self._repo_info_by_actor.get(actor.name)
        if info is None:
            self._kick_repo_info_build(actor)

        # ── icon panel (left of card) ──────────────────────────────
        # Actor name's first letter as a 3-row LCD-style monogram
        # (Textual's `DigitsRenderable` only ships A-F + digits;
        # we keep our own full-alphabet table in `_LCD_LETTERS` so
        # any actor name's initial renders consistently).
        initial = (actor.name[:1] or "?").upper()
        shape = self._LCD_LETTERS.get(initial)
        if shape is None:
            icon_body = Text(
                initial,
                style=f"bold {colors['primary']}",
                justify="center",
            )
        else:
            # Rich's `justify="center"` strips trailing spaces per
            # line before centering, which collapses rows like `│  `
            # to `│` and pushes them to the middle of the panel.
            # Substituting non-breaking spaces preserves the column
            # alignment without affecting the visible glyph.
            # Centering via `justify="center"` trims trailing spaces
            # (Rich's per-line rstrip — NBSP counts as whitespace
            # too, so U+00A0 substitution doesn't help). Pad each
            # line on the LEFT so leading whitespace (which Rich
            # doesn't strip) does the centering for us.
            #
            # Panel content width: 9 - 2 border - 2 padding = 5.
            # LCD letters are 3 cols wide → 1 col of leading pad
            # centers them.
            _icon_inner = 5
            letter_w = max(len(line) for line in shape)
            left_pad = (_icon_inner - letter_w + 1) // 2
            padded = [(" " * left_pad) + line for line in shape]
            icon_body = Text(
                "\n".join(padded),
                style=f"bold {colors['primary']}",
            )
        icon_panel = Panel(
            icon_body,
            width=9,
            height=5,
            border_style=colors["primary"],
            box=rich_box.ROUNDED,
            padding=(0, 1),
        )

        # ── title row: name + status icon (mirrors the actor list) ──
        # Same RUNNING_FRAMES animation cycle the tree uses, sampled
        # off the tree's `_anim_frame` so the two surfaces share one
        # source of truth and stay roughly in phase. Other statuses
        # use the static STATUS_ICON glyph (DONE / IDLE map to "" so
        # the row falls back to just the name). Icon inherits the
        # title's secondary color so the row reads as one composite
        # glyph instead of fighting between two palette slots.
        title_row = Text()
        title_row.append(
            actor.name.upper(),
            style=f"bold {colors['secondary']}",
        )
        icon = self._overview_status_icon(status)
        if icon:
            title_row.append(f" {icon}", style=colors["secondary"])

        # ── first metadata strip: MODEL / LAST ACTIVITY / LOCATION ─
        agent_line = self._format_agent_line(actor)
        in_tok, out_tok = self._aggregate_token_usage(actor.name)
        if in_tok or out_tok:
            agent_line += (
                f" · {self._humanize_count(in_tok)} ⇡ "
                f"{self._humanize_count(out_tok)} ⇣"
            )
        meta1 = Table.grid(expand=True, padding=(0, 2))
        meta1.add_column(ratio=1)
        meta1.add_column(ratio=1)
        meta1.add_row(
            self._labeled_field("MODEL", agent_line),
            self._labeled_field(
                "LAST ACTIVITY",
                self._format_age_line(actor, status),
            ),
        )

        # Location row — folder Nerd icon + path, no label.
        # Collapse $HOME → ~ for display compactness.
        from pathlib import Path
        display_dir = actor.dir or ""
        home = str(Path.home())
        if display_dir.startswith(home):
            display_dir = "~" + display_dir[len(home):]
        location_line = Text(no_wrap=True, overflow="ellipsis")
        location_line.append("󰉋  ", style="dim")
        location_line.append(display_dir or "—")

        # ── second metadata strip: BRANCH on row 1, PR on row 2 ────
        diff_counts = self._diff_counts_by_actor.get(actor.name)
        meta2 = Group(
            self._build_branch_field(actor, info, diff_counts, colors),
            self._build_pr_field(info, colors),
        )

        # ── extra-config line (optional) ───────────────────────────
        # Keys already shown in the MODEL line (model / m / use-
        # subscription) are filtered out so we don't echo them.
        # Everything else from agent_args + actor_keys lands here.
        config_line = self._build_config_field(actor)

        # ── compose right side stack ───────────────────────────────
        sections: list = [
            title_row,
            Text(""),
            meta1,
            Rule(style="dim"),
            Text(""),  # blank between rule and location row
            location_line,
            meta2,
        ]
        if config_line is not None:
            # Blank row separates the BRANCH/PR block above from the
            # CONFIG row — keeps the eye from grouping `permission-
            # mode=plan` with the PR line. Only inserted when there
            # IS a config row, otherwise the card ends flush.
            sections.append(Text(""))
            sections.append(config_line)
        right = Group(*sections)

        # ── outer two-col: icon | right ────────────────────────────
        outer = Table.grid(expand=True, padding=(0, 2))
        outer.add_column(width=9)
        outer.add_column(ratio=1)
        outer.add_row(icon_panel, right)

        card = Panel(
            outer,
            border_style="dim",
            box=rich_box.ROUNDED,
            padding=(0, 2, 1, 2),
        )

        try:
            widget = self.query_one("#overview-header", Static)
        except Exception:
            return
        widget.update(card)

    @staticmethod
    def _labeled_field(label: str, value):
        """Two-line cell used inside the header card: a small uppercase
        muted label on top, the actual datum below. `value` may be a
        plain string or a Rich Text — we keep the param loose so each
        caller can colorize its own datum."""
        from rich.console import Group
        from rich.text import Text

        label_text = Text(label, style="dim")
        if isinstance(value, str):
            value = Text(value, no_wrap=True, overflow="ellipsis")
        else:
            # Existing Rich Text — also force single-line with
            # ellipsis overflow so MODEL · subscription · token-counts
            # stays on one line even when the panel narrows.
            value.no_wrap = True
            value.overflow = "ellipsis"
        return Group(label_text, value)

    @staticmethod
    def _build_branch_field(
        actor: Actor,
        info: dict | None,
        diff_counts: tuple[int, int] | None,
        colors: dict[str, str],
    ):
        """The BRANCH cell: `branchname → destination` (destination
        muted), or just `branchname` when the actor's branch IS the
        base/destination (i.e. it's on the main branch already).

        We compare the live branch to `info["base"]` (or
        `actor.base_branch` while the worker hasn't returned yet) —
        the recorded base from `actor new`. That covers the
        "checked out main directly" case without needing an extra
        git query for the repo's HEAD default branch.

        `diff_counts` is the same `(added, removed)` tuple the DIFF
        tab label uses, sourced from `_diff_counts_by_actor`. When
        present and non-zero, render it inline with success/error
        colors so the OVERVIEW agrees with the tab label."""
        from rich.text import Text

        t = Text()
        t.append("󰘬  ", style="dim")
        if info is None:
            t.append(actor.name)
            return t
        branch = info.get("branch") or actor.name
        base = info.get("base") or actor.base_branch
        t.append(branch)
        # `*` immediately after the branch name when there are
        # uncommitted changes — same convention `git status -sb` uses.
        if info.get("dirty"):
            t.append("*")
        # `+N -M` line counts shared with the DIFF tab label. Colors
        # live here only — the tab label stays plain. Wrapped in
        # muted square brackets so the colored numbers read as a
        # bracketed annotation rather than free-floating tokens.
        added, removed = diff_counts or (0, 0)
        if added or removed:
            t.append(" [", style="dim")
            if added:
                t.append(f"+{added}", style=colors["success"])
            if added and removed:
                t.append(" ")
            if removed:
                t.append(f"-{removed}", style=colors["error"])
            t.append("]", style="dim")
        if base and branch != base:
            t.append(" → ", style="dim")
            t.append(base, style="dim")
        return t

    def _build_pr_field(
        self, info: dict | None, colors: dict[str, str],
    ):
        """The WORKTREE STATUS cell on the right: GitHub icon + PR
        number/state, or "(none)" when there's no PR for this branch."""
        from rich.text import Text

        t = Text()
        t.append("󰊤  ", style="dim")
        if info is None:
            t.append("loading…", style="dim italic")
            return t
        pr_num = info.get("pr_number")
        if isinstance(pr_num, int):
            url = info.get("pr_url") or ""
            # Terminal hyperlink: Rich's `link` style wraps the
            # rendered span in OSC-8 escapes; clickable in
            # iTerm2 / WezTerm / Kitty / Ghostty.
            link_style = f"{colors['primary']}"
            if url:
                link_style += f" link {url}"
            t.append(f"#{pr_num}", style=link_style)
            state = info.get("pr_state")
            if state:
                t.append(
                    f"  ({state.lower()})",
                    style=self._pr_state_color(state, colors),
                )
        else:
            t.append("no PR yet", style="dim")
        return t

    # Keys already surfaced in the MODEL line — filtered out of the
    # extra-config row so we don't repeat them. Both Claude (`model`)
    # and Codex (short flag `m`) are accounted for.
    _CONFIG_KEYS_SHOWN_ELSEWHERE: frozenset[str] = frozenset({
        "model", "m", "use-subscription",
    })

    @classmethod
    def _build_config_field(cls, actor: Actor):
        """Optional CONFIG row: agent_args / actor_keys that DIVERGE
        from both `_CONFIG_KEYS_SHOWN_ELSEWHERE` (already echoed by
        the MODEL line) and the agent class's hardcoded defaults
        (`AGENT_DEFAULTS` + `ACTOR_DEFAULTS`). Returns None when
        nothing remains so the header skips the row entirely.

        The class-defaults filter is what makes this row meaningful —
        `permission-mode=auto` on a vanilla Claude actor isn't 'extra
        config', it's the baseline. We only want to surface things
        the user actually customized (or that a role / kdl set
        explicitly to something non-default)."""
        from rich.text import Text

        from ..commands import _agent_class
        agent_cls = _agent_class(actor.agent)
        defaults: dict[str, str] = {}
        defaults.update(getattr(agent_cls, "AGENT_DEFAULTS", {}) or {})
        defaults.update(getattr(agent_cls, "ACTOR_DEFAULTS", {}) or {})

        merged: dict[str, str] = {}
        merged.update(actor.config.actor_keys)
        # agent_args wins on collision — same precedence as the
        # underlying ActorConfig (actor_keys is a separate namespace
        # but the union here is just for display, not behavior).
        merged.update(actor.config.agent_args)
        extras = [
            (k, v) for k, v in sorted(merged.items())
            if k not in cls._CONFIG_KEYS_SHOWN_ELSEWHERE
            and defaults.get(k) != v
        ]
        if not extras:
            return None
        t = Text(no_wrap=True, overflow="ellipsis", style="dim")
        # Material Design `cog` (U+F0493). Double-space after, like the
        # branch (󰘬) and folder (󰉋) rows above. Whole row uses Rich's
        # `dim` style — config is a low-priority detail next to MODEL /
        # BRANCH / PR.
        t.append("\U000F0493  ")
        for i, (k, v) in enumerate(extras):
            if i:
                t.append(" · ")
            t.append(k)
            if v != "":
                t.append("=")
                t.append(v)
        return t

    def _aggregate_token_usage(self, actor_name: str) -> tuple[int, int]:
        """Sum (input_tokens, output_tokens) across all assistant
        messages in the cached log entries for this actor.

        Only `input_tokens` is summed on the input side — i.e. the
        non-cached portion of the prompt. Adding `cache_creation_*`
        and `cache_read_*` would technically reflect the full context
        size the model saw, but reads as confusingly large for a user
        who just typed a short prompt: every Claude Code session
        carries ~40K tokens of system-prompt/skill/tool bootstrap
        that gets re-counted via cache_read on every subsequent turn.
        Sticking to `input_tokens` keeps the displayed number close
        to "what I actually sent this turn".

        `usage` is attached to at most one LogEntry per JSONL message
        (see `ClaudeAgent._parse_log_dict`), so a straight sum is
        already deduped against multi-block messages. Returns
        (0, 0) when no usage info is present (e.g. codex sessions
        until that parser gets analogous plumbing)."""
        entries = self._log_entries_by_actor.get(actor_name, [])
        in_tok = 0
        out_tok = 0
        for e in entries:
            usage = e.usage
            if not isinstance(usage, dict):
                continue
            in_val = usage.get("input_tokens")
            if isinstance(in_val, int):
                in_tok += in_val
            out_val = usage.get("output_tokens")
            if isinstance(out_val, int):
                out_tok += out_val
        return in_tok, out_tok

    @staticmethod
    def _humanize_count(n: int) -> str:
        """Compact rendering for token counts: 1234 → 1.2K,
        1_234_567 → 1.2M. Anything under 1000 prints as-is so small
        sessions don't read as 0.5K."""
        if n < 1000:
            return str(n)
        if n < 1_000_000:
            return f"{n / 1000:.1f}K"
        return f"{n / 1_000_000:.1f}M"

    def _overview_palette(self) -> dict[str, str]:
        """Resolve theme variables to concrete hex colors for use in
        Rich Style strings. Rich doesn't understand Textual's
        `$primary` / `$success` / etc. variables — those only resolve
        inside Textual CSS. We pull the same values out of the active
        theme here so omarchy flavor flips still land on the OVERVIEW
        sections.

        Falls back to brand defaults when `current_theme` raises
        before super().__init__ runs (some bare-construct tests)."""
        try:
            t = self.current_theme
        except Exception:
            t = None
        return {
            "primary": (t.primary if t else None) or "#B1B9F9",
            "secondary": (t.secondary if t else None) or "#D77757",
            "success": (t.success if t else None) or "#4EBA65",
            "error": (t.error if t else None) or "#FF6B80",
        }

    def _overview_status_icon(self, status: Status) -> str:
        """Pick the right status glyph for the OVERVIEW header. RUNNING
        uses the tree's animation frame (so both surfaces tick in
        phase); other statuses use the static STATUS_ICON map. Returns
        '' for statuses that have no glyph (DONE / IDLE), so callers
        can omit the icon entirely rather than rendering a blank cell."""
        from .helpers import STATUS_ICON
        from .tree import RUNNING_FRAMES
        if status == Status.RUNNING:
            try:
                tree = self.query_one(ActorTree)
                return RUNNING_FRAMES[tree._anim_frame % len(RUNNING_FRAMES)]
            except Exception:
                return RUNNING_FRAMES[0]
        return STATUS_ICON.get(status, "")

    @staticmethod
    def _status_pill(
        status: Status, colors: dict[str, str],
    ) -> tuple[str, str]:
        """Return (color, label) for the status pill on the right of
        the header. The color is a resolved hex from the palette
        dict so Rich can apply it without going through Textual's
        CSS variable layer."""
        mapping = {
            Status.RUNNING: (colors["primary"], "RUNNING"),
            Status.DONE: (colors["success"], "DONE"),
            Status.ERROR: (colors["error"], "ERROR"),
            Status.IDLE: ("dim", "IDLE"),
        }
        return mapping.get(status, ("dim", status.value.upper()))

    @staticmethod
    def _format_agent_line(actor: Actor) -> str:
        """Build the `agent · model · auth` subtitle. Pulls model from
        the resolved config; auth from the use-subscription actor
        key. Falls back to bare agent name when bits are missing."""
        agent = actor.agent.value
        model = actor.config.agent_args.get("model")
        if not model:
            # Codex stores the model under `m` (short flag).
            model = actor.config.agent_args.get("m")
        use_sub = actor.config.actor_keys.get("use-subscription", "true")
        auth = "subscription" if use_sub != "false" else "api key"
        parts = [agent]
        if model:
            parts.append(model)
        parts.append(auth)
        return " · ".join(parts)

    @staticmethod
    def _format_age_line(actor: Actor, status: Status) -> str:
        """LAST ACTIVITY line: `<DD Mon YYYY HH:MM> (<X units ago>)`.

        Local timezone, time to the minute, date as day-month-year.
        Anchor:
          - RUNNING → most recent run's started_at (the ongoing run).
          - other  → most recent run's finished_at, falling back to
                     actor.created_at if no runs have completed yet.
        Returns `—` when no anchor is resolvable."""
        from datetime import datetime, timezone
        from ..types import _parse_iso

        now = datetime.now(timezone.utc)
        if status == Status.RUNNING:
            anchor = ActorWatchApp._most_recent_run_started_at(actor)
            if anchor is None:
                anchor = _parse_iso(actor.created_at)
        else:
            anchor = (
                ActorWatchApp._most_recent_run_finished_at(actor)
                or _parse_iso(actor.created_at)
            )
        if anchor is None:
            return "—"
        local = anchor.astimezone()
        # `%-d` is GNU/BSD; on Windows you'd use `%#d`. actor.sh is
        # POSIX-only (Textual + pty.fork in src/actor/watch/interactive),
        # so stick with `%-d` rather than padding-to-two-digits.
        date_part = local.strftime("%-d %b %Y %H:%M")
        delta = (now - anchor).total_seconds()
        return f"{date_part} ({ActorWatchApp._humanize_relative(delta)})"

    @staticmethod
    def _humanize_relative(seconds: float) -> str:
        """`<N> <unit> ago` for minutes / hours / days. Sub-minute
        deltas elide the number — "seconds ago" reads cleaner than a
        digit that would be ticking in real time alongside an HH:MM
        timestamp that doesn't change. Floors values (no rounding-up
        surprises) and clamps negative deltas to zero."""
        s = max(0, int(seconds))
        if s < 60:
            return "seconds ago"
        if s < 3600:
            n = s // 60
            return f"{n} minute{'s' if n != 1 else ''} ago"
        if s < 86400:
            n = s // 3600
            return f"{n} hour{'s' if n != 1 else ''} ago"
        n = s // 86400
        return f"{n} day{'s' if n != 1 else ''} ago"

    @staticmethod
    def _most_recent_run_started_at(actor: Actor):
        from ..types import _parse_iso
        with Database.open(_db_path()) as db:
            runs, _total = db.list_runs(actor.name, limit=1)
        if not runs:
            return None
        return _parse_iso(runs[0].started_at)

    @staticmethod
    def _most_recent_run_finished_at(actor: Actor):
        from ..types import _parse_iso
        with Database.open(_db_path()) as db:
            runs, _total = db.list_runs(actor.name, limit=1)
        if not runs:
            return None
        return _parse_iso(runs[0].finished_at) if runs[0].finished_at else None

    @staticmethod
    def _humanize_seconds(seconds: float) -> str:
        """Human-readable duration — 'Xs' under a minute, 'XmYs'
        under an hour, 'XhYm' otherwise. Negative values clamp to
        zero (clock skew between actor host and watch host is
        possible if those ever diverge)."""
        s = max(0, int(seconds))
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"

    def _tick_overview_age(self) -> None:
        """1Hz refresh of the OVERVIEW header, last-interaction
        timestamps, and the runs table — keeps everything that
        depends on wall-clock time live: the 'running for Xs'
        counter, the 'Xs ago' meta lines, the in-flight row in the
        recent-runs table. No-ops when no actor is selected. Each
        section is one Static / DataTable update with a small Rich
        renderable; cumulative cost stays well under the 1Hz budget."""
        actor = self._overview_header_actor
        if actor is None:
            return
        status = self._prev_statuses.get(actor.name, Status.IDLE)
        self._render_overview_header(actor, status)
        self._render_last_interaction(actor)
        # Only re-render the runs table while a run is in flight —
        # otherwise nothing changes per second.
        if any(
            r == Status.RUNNING
            for r in self._prev_statuses.values()
        ):
            self._refresh_runs(actor)

    def _tick_overview_running_icon(self) -> None:
        """Re-render the OVERVIEW header at the tree's animation
        cadence (0.5s) ONLY while the selected actor is RUNNING — so
        the spinner glyph advances frame-by-frame, in phase with the
        actor list, without paying the cost of a full header re-render
        twice a second when nothing's animating."""
        actor = self._overview_header_actor
        if actor is None:
            return
        status = self._prev_statuses.get(actor.name, Status.IDLE)
        if status != Status.RUNNING:
            return
        self._render_overview_header(actor, status)

    def _render_last_interaction(self, actor: Actor) -> None:
        """Build the side-by-side `LAST PROMPT` / `LAST RESPONSE`
        panels in the OVERVIEW pane.

        Source is `_log_entries_by_actor[actor.name]` — the same
        cache the LIVE pane reads from, so we don't pay for a
        second JSONL parse. While LIVE has never been visited for
        this actor the cache is empty; we still render the panel
        scaffolding (with a 'no activity yet' line) so the user sees
        the section exists. The next log poll will fill it in.

        Entries are walked backwards: first user-prompt-with-string
        is the latest user prompt, first assistant-with-text is the
        latest agent response. Tool-result user messages and
        thinking blocks are skipped."""
        from rich import box as rich_box
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        entries = self._log_entries_by_actor.get(actor.name, [])
        last_prompt, prompt_meta = self._last_user_prompt(entries)
        last_response, response_meta = self._last_assistant_text(entries)
        colors = self._overview_palette()

        # Panel title only accepts Text/str (Rich calls .copy() on it),
        # so we build a Text with the icon + label and put the
        # "Xs ago" timestamp in `subtitle` aligned right. Visually:
        #   ╭ LAST PROMPT ──────────────────────╮
        #   │  body                             │
        #   ╰─────────────────────── 5m ago ────╯
        def _title(icon: str, label: str, color: str) -> Text:
            t = Text()
            t.append(f"{icon}  ", style=color)
            t.append(label, style=f"bold {color}")
            return t

        prompt_body = Text(
            self._truncate_for_panel(last_prompt) if last_prompt
            else "(no prompt yet)",
            style="" if last_prompt else "dim",
        )
        response_body = Text(
            self._truncate_for_panel(last_response) if last_response
            else "(awaiting first response)",
            style="" if last_response else "dim",
        )

        prompt_panel = Panel(
            prompt_body,
            title=_title("", "LAST PROMPT", colors["primary"]),
            title_align="left",
            subtitle=Text(prompt_meta, style="dim") if prompt_meta else None,
            subtitle_align="right",
            border_style=colors["primary"],
            box=rich_box.ROUNDED,
            padding=(1, 2),
        )
        response_panel = Panel(
            response_body,
            title=_title("", "LAST RESPONSE", colors["success"]),
            title_align="left",
            subtitle=Text(response_meta, style="dim") if response_meta else None,
            subtitle_align="right",
            border_style=colors["success"],
            box=rich_box.ROUNDED,
            padding=(1, 2),
        )

        # Two-column grid that stretches to widget width.
        layout = Table.grid(expand=True, padding=(0, 1))
        layout.add_column(ratio=1)
        layout.add_column(ratio=1)
        layout.add_row(prompt_panel, response_panel)

        try:
            widget = self.query_one("#overview-last-interaction", Static)
        except Exception:
            return
        widget.update(layout)

    # -- Repo info section ---------------------------------------------------

    # `_repo_info_by_actor[name] = dict` cached per actor. Populated by
    # `_refresh_repo_info` worker; consumed by `_render_repo_info`.
    # Keys: dir, worktree, branch, base, ahead, dirty, pr_number,
    # pr_state, pr_url. Missing values are None — render handles each
    # absence individually so the section degrades gracefully when (e.g.)
    # gh isn't installed.
    _repo_info_by_actor: dict[str, dict] = {}
    # Session_id last seen per actor name — same trick the log cache
    # uses to invalidate on discard+recreate so a new actor doesn't
    # inherit the prior incarnation's repo info.
    _repo_info_session_for: dict[str, str] = {}

    @staticmethod
    def _pr_state_color(state: str, palette: dict[str, str]) -> str:
        s = state.lower()
        if s == "open":
            return palette["success"]
        if s == "merged":
            return palette["primary"]
        if s == "closed":
            return palette["error"]
        return "dim"

    def _kick_repo_info_build(self, actor: Actor) -> None:
        """Schedule the off-thread `_refresh_repo_info` worker for
        the given actor. Fires on first render (when cache is empty)
        and from `_refresh_detail` when the session_id changes.
        Re-runs are cheap-ish (3 git subprocs + 1 gh subproc) and the
        worker is `exclusive=True group="repo_info"` so a newer kick
        cancels in-flight ones cleanly."""
        # Record the session_id we're about to fetch info for; the
        # apply path checks this against the live actor before
        # committing to avoid stomping a newer kick's result.
        self._repo_info_session_for[actor.name] = actor.agent_session or ""
        self._refresh_repo_info(actor)

    @work(thread=True, exclusive=True, group="repo_info")
    def _refresh_repo_info(self, actor: Actor) -> None:
        info = self._gather_repo_info(actor)
        self.call_from_thread(self._apply_repo_info, actor, info)

    @staticmethod
    def _gather_repo_info(actor: Actor) -> dict:
        """Run the git + gh queries that populate the repo section.
        Pure function — safe to call from any thread; touches no
        widget state."""
        import subprocess
        info: dict = {
            "dir": actor.dir,
            "worktree": bool(actor.worktree),
            "branch": None,
            "base": actor.base_branch,
            "ahead": None,
            "dirty": None,
            "pr_number": None,
            "pr_state": None,
            "pr_url": None,
        }

        def run(cmd: list[str]) -> tuple[int, str]:
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True,
                    cwd=actor.dir, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                return -1, ""
            return r.returncode, (r.stdout or "")

        # Branch (verify / pull current — usually matches actor.name).
        rc, out = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if rc == 0 and out.strip():
            info["branch"] = out.strip()

        # Commits ahead of base.
        if actor.base_branch:
            rc, out = run([
                "git", "rev-list", "--count",
                f"{actor.base_branch}..HEAD",
            ])
            if rc == 0 and out.strip().isdigit():
                info["ahead"] = int(out.strip())

        # Dirty file count — modified, staged, AND untracked
        # (excluding ignored). `--porcelain` produces one line per
        # path; counting lines is the cheapest portable signal.
        rc, out = run([
            "git", "status", "--porcelain", "--untracked-files=all",
        ])
        if rc == 0:
            info["dirty"] = sum(1 for line in out.splitlines() if line.strip())

        # PR via gh — silently skip when gh isn't on PATH or returns
        # an error (no PR for this branch is the common case).
        if info["branch"]:
            rc, out = run([
                "gh", "pr", "list",
                "--head", info["branch"],
                "--state", "all",
                "--json", "number,state,url",
                "--limit", "1",
            ])
            if rc == 0 and out.strip():
                import json
                try:
                    items = json.loads(out)
                except json.JSONDecodeError:
                    items = []
                if items:
                    pr = items[0]
                    info["pr_number"] = pr.get("number")
                    info["pr_state"] = pr.get("state")
                    info["pr_url"] = pr.get("url")

        return info

    def _apply_repo_info(self, actor: Actor, info: dict) -> None:
        """Main-thread commit of repo info. Drops silently when the
        actor's session_id has moved on since the worker started
        (e.g. user switched actors before the gh call returned)."""
        recorded = self._repo_info_session_for.get(actor.name)
        if recorded != (actor.agent_session or ""):
            return
        self._repo_info_by_actor[actor.name] = info
        # Repo info now lives inside the header; re-render the header
        # only when the selected actor matches what the worker fetched
        # (otherwise the user has moved on).
        current = self._overview_header_actor
        if current is not None and current.name == actor.name:
            status = self._prev_statuses.get(current.name, Status.IDLE)
            self._render_overview_header(current, status)

    @staticmethod
    def _last_user_prompt(entries: list) -> tuple[str, str]:
        """Walk entries backwards; return (text, meta) for the most
        recent user-typed prompt. Tool-result user messages have
        kind=TOOL_RESULT in our parsed entries, so `kind == USER`
        already filters those out."""
        for e in reversed(entries):
            if e.kind == LogEntryKind.USER and e.text:
                return e.text, ActorWatchApp._format_relative(e.timestamp)
        return "", ""

    @staticmethod
    def _last_assistant_text(entries: list) -> tuple[str, str]:
        """Walk entries backwards for the latest assistant TEXT
        block (not thinking, not tool_use). Real responses are what
        the user wants to see; tool calls are mid-flight detail."""
        for e in reversed(entries):
            if e.kind == LogEntryKind.ASSISTANT and e.text:
                return e.text, ActorWatchApp._format_relative(e.timestamp)
        return "", ""

    @staticmethod
    def _truncate_for_panel(text: str, max_lines: int = 8, max_chars: int = 600) -> str:
        """Soft-truncate a message body for the OVERVIEW panel.
        Limits both line count and total characters — markdown
        prompts can be very long single lines, while agent responses
        can be very wide multi-line; both shapes need taming."""
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + " …"
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines.append("…")
        return "\n".join(lines)

    @staticmethod
    def _format_relative(timestamp: str | None) -> str:
        """Format an ISO timestamp as 'Xs ago' / 'Xm ago' / 'XhYm
        ago'. Returns empty string when the input is None or
        unparseable so the caller can drop the meta line cleanly."""
        if not timestamp:
            return ""
        from datetime import datetime, timezone
        from ..types import _parse_iso

        when = _parse_iso(timestamp)
        if when is None:
            return ""
        delta = (datetime.now(timezone.utc) - when).total_seconds()
        return f"{ActorWatchApp._humanize_seconds(delta)} ago"

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
            # Detect session_id change for this actor name. Happens when
            # the user discards-and-recreates an actor with the same
            # name: the cached entries belong to the prior incarnation's
            # JSONL file, the cursor is a byte offset into that file,
            # and neither has anything to say about the new session.
            # Without this reset, `_append_logs` would extend the new
            # actor's entries onto the old actor's bucket and the user
            # sees a concatenated transcript across both lifetimes.
            last_session = self._log_session_for_actor.get(actor.name)
            if last_session != actor.agent_session:
                self._log_session_for_actor[actor.name] = actor.agent_session
                self._log_entries_by_actor.pop(actor.name, None)
                self._log_cursors.pop(actor.name, None)
                # Defer clearing _last_log_* to the main thread via
                # _append_logs (which runs there); the worker doesn't
                # touch widget state.
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
    # Tracks the session_id last seen for each actor name so a new
    # session under the same name (after discard + recreate) is
    # detected and the stale entries/cursor are dropped before the
    # new file's bytes are parsed onto the previous transcript.
    _log_session_for_actor: dict[str, str] = {}
    _log_lock = threading.Lock()

    _last_log_actor: str | None = None
    _last_log_count: int = 0
    _last_log_width: int = 0
    _last_log_entries: list = []

    # Calls to _set_logs that arrive while the RichLog is collapsed
    # (zero width) stash here for replay once the widget has a real
    # width. Kept separate from _last_log_* so that the short-circuit
    # logic — which assumes _last_log_* reflects what the widget has
    # actually committed — doesn't wrongly skip a render after a
    # zero-width call set _last_log_actor=actor without rendering.
    _pending_log_actor: str | None = None
    _pending_log_entries: list = []

    # Full-rebuild builds run in a worker thread. `_log_build_token`
    # increments on every kick-off; the worker's apply callback is a
    # no-op if a newer build has started in the meantime. `_pending`
    # is True while a build is in flight — guards append-path
    # decisions (which assume the widget reflects committed state) and
    # gates the 300ms placeholder. `_target_*` records what the
    # in-flight build is aiming at so that a follow-up _set_logs call
    # carrying the same entries (e.g. a 2s poll with no new activity)
    # can short-circuit instead of endlessly re-kicking and starving
    # the build of a chance to commit.
    _log_build_token: int = 0
    _log_build_pending: bool = False
    _log_build_target_actor: str | None = None
    _log_build_target_count: int = 0
    _log_build_target_width: int = 0

    def on_resize(self) -> None:
        log = self.query_one("#logs-content", RichLog)
        if log.size.width != self._last_log_width and self._last_log_entries:
            self._last_log_count = 0
            self._set_logs(self._last_log_actor, self._last_log_entries)

        # Diff: width is part of the cache key, so a real width change
        # invalidates the last apply. Re-kick for the currently-loaded
        # actor; the kick's own width-0 guard handles the hidden case.
        try:
            scroll = self.query_one("#diff-scroll", VerticalScroll)
        except Exception:
            return
        last_key = self._diff_last_applied_key
        if last_key is None:
            return
        last_actor_name, _last_oid, last_width = last_key
        if scroll.size.width == last_width:
            return
        actor = next(
            (a for a in self._current_actors if a.name == last_actor_name),
            None,
        )
        if actor is None:
            return
        self._kick_diff_build(actor)

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
        # If this actor is the currently-selected one, refresh the
        # OVERVIEW last-interaction panels so the latest user prompt
        # / agent response appear without waiting for the 1Hz tick.
        if (
            self._overview_header_actor is not None
            and self._overview_header_actor.name == actor_name
        ):
            self._render_last_interaction(self._overview_header_actor)

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
        # is actually visible. _last_log_* must NOT change here — it
        # would lie about what the widget has committed and cause the
        # next call to short-circuit a render that never happened.
        if log.size.width == 0:
            self._pending_log_actor = actor_name
            self._pending_log_entries = entries
            return

        # We have a real width — clear any pending zero-width stash;
        # this call (and the _last_log_* update on its render path)
        # supersedes whatever was waiting.
        self._pending_log_actor = None
        self._pending_log_entries = []

        actor_changed = actor_name != self._last_log_actor
        width_changed = log.size.width != self._last_log_width
        # Nothing-changed shortcut. When a full build is in flight the
        # "committed" baseline is `_last_log_*`, but the meaningful
        # baseline for skipping is the in-flight build's TARGET — if
        # this call carries the same actor/entries/width the build is
        # already working on, re-kicking would only cancel its own
        # progress. Compare against target while pending; against
        # committed state otherwise.
        if self._log_build_pending:
            if (
                actor_name == self._log_build_target_actor
                and log.size.width == self._log_build_target_width
                and len(entries) == self._log_build_target_count
            ):
                return
        elif (
            not actor_changed
            and not width_changed
            and entries is self._last_log_entries
            and len(entries) == self._last_log_count
        ):
            return

        prior_count = self._last_log_count
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
        #   - no full build currently in flight (committed state
        #     matches the widget)
        #   - same actor (prior_count + entries refer to the same stream)
        #   - same bucket object (a discard+recreate or `actor run`
        #     under the same actor name pops and rebuilds the bucket;
        #     appending the new entries onto the widget that still
        #     holds the old session's render concatenates two
        #     transcripts under one name — full rebuild instead)
        #   - same width (RichLog's cached segments are width-specific)
        #   - entry list only grew (prior_count <= new_count)
        #   - the new tail contains no tool_result (if it did, it might
        #     pair with a tool_use we already wrote to the log — RichLog
        #     is append-only, we can't patch the old tool's rendered
        #     row in place, so we fall back to a full rerender)
        can_append = (
            not self._log_build_pending
            and not actor_changed
            and entries is self._last_log_entries
            and not width_changed
            and 0 <= prior_count <= len(entries)
            and not self._tail_has_tool_result(entries, prior_count)
        )
        if can_append:
            append_log_entries(log, entries, prior_count, colors)
            self._last_log_actor = actor_name
            self._last_log_count = len(entries)
            self._last_log_width = log.size.width
            self._last_log_entries = entries
            if at_bottom:
                log.scroll_end(animate=False)
        else:
            self._kick_off_full_build(
                actor_name, entries, colors, log.size.width, at_bottom,
            )

    def _kick_off_full_build(
        self,
        actor_name: str,
        entries: list,
        colors: ThemeColors,
        width: int,
        at_bottom: bool,
    ) -> None:
        """Start an off-thread build of the full RichLog content. A
        300ms timer shows the "Loading logs..." placeholder if the
        build hasn't committed by then — short builds commit first and
        the placeholder never flashes."""
        self._log_build_token += 1
        self._log_build_pending = True
        self._log_build_target_actor = actor_name
        self._log_build_target_count = len(entries)
        self._log_build_target_width = width
        token = self._log_build_token

        def _maybe_show_placeholder() -> None:
            # Fire only if this build is still the latest one AND
            # hasn't already applied. Any newer kick-off bumps the
            # token; a committed apply clears the pending flag.
            if token != self._log_build_token or not self._log_build_pending:
                return
            try:
                log = self.query_one("#logs-content", RichLog)
            except Exception:
                return
            log.clear()
            log.write(Text("Loading logs...", style="dim"))

        self.set_timer(0.3, _maybe_show_placeholder)
        self._build_log_worker(
            token, actor_name, entries, colors, width, at_bottom,
        )

    @work(thread=True, exclusive=True, group="log_build")
    def _build_log_worker(
        self,
        token: int,
        actor_name: str,
        entries: list,
        colors: ThemeColors,
        width: int,
        at_bottom: bool,
    ) -> None:
        # Cooperative cancel: when a newer build supersedes this one,
        # `_log_build_token` is already the new value on the main
        # thread. Check each tick and bail early so we don't keep
        # burning CPU (or, for the test-harness artificial sleep,
        # keep sleeping) on output that will be discarded anyway.
        def is_cancelled() -> bool:
            return self._log_build_token != token

        renderables = build_log_renderables(entries, colors, is_cancelled)
        if renderables is None:
            # Cancelled mid-build; the newer worker owns the apply.
            return
        self.call_from_thread(
            self._apply_log_build,
            token, actor_name, entries, renderables, width, at_bottom,
        )

    def _apply_log_build(
        self,
        token: int,
        actor_name: str,
        entries: list,
        renderables: list,
        width: int,
        at_bottom: bool,
    ) -> None:
        if token != self._log_build_token:
            # A newer build kicked off while this one was in the
            # worker. Discard the stale output — the newer build's
            # apply will land the correct state.
            return
        try:
            log = self.query_one("#logs-content", RichLog)
        except Exception:
            self._log_build_pending = False
            return
        apply_log_renderables(log, renderables)
        self._log_build_pending = False
        self._last_log_actor = actor_name
        self._last_log_count = len(entries)
        self._last_log_width = width
        self._last_log_entries = entries
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
            # Selection cleared (e.g. previously-selected actor was
            # discarded with no replacement). Wipe the badge so the
            # tabs bar doesn't display stale ±N from the prior actor.
            if self._diff_loaded_for is not None:
                self._update_diff_tab_label()
                self._diff_loaded_for = None
            return
        if not force and self._diff_loaded_for == actor.name:
            return
        # Actor change: reset the badge before kicking. If the new
        # actor's shortstat call fails (broken base ref, no repo,
        # etc.) `_apply_diff_badge` is never called and the previous
        # actor's ±N would otherwise stick on the label. Clearing
        # here means a failed kick falls back to plain "DIFF" rather
        # than misleading numbers from a different worktree.
        if self._diff_loaded_for != actor.name:
            self._update_diff_tab_label()
        self._diff_loaded_for = actor.name
        # Kick both paths together. The badge is independent of the
        # full build — even if the build is stashed because DIFF is
        # hidden, the badge still fires and updates the always-visible
        # tab label. `force` propagates to the build kick so the
        # cache-key short-circuit doesn't suppress live-poll-driven
        # refreshes (HEAD doesn't move during a typical run, so the
        # cache key matches, but the worktree HAS changed).
        self._kick_diff_badge(actor)
        self._kick_diff_build(actor, force=force)

    def _kick_diff_badge(self, actor: Actor) -> None:
        """Fire a quick `git diff --shortstat` worker just to populate
        the DIFF (±N) tab label. Independent of the full build path
        so the label appears in <100ms even when the full render is
        still parsing diffs and building Tables.

        Fires regardless of whether the DIFF tab is currently active —
        the label sits in the tabs bar, which is always visible, so a
        user looking at LIVE still benefits from seeing what's pending
        on DIFF. (The full build path's hidden-tab stash is about
        avoiding zero-width RichLog renders; the badge has no widget
        to render into, so the same constraint doesn't apply.)"""
        self._diff_badge_token += 1
        self._diff_badge_target_actor = actor.name
        self._build_diff_badge_worker(self._diff_badge_token, actor)

    @work(thread=True, exclusive=True, group="diff_badge")
    def _build_diff_badge_worker(self, token: int, actor: Actor) -> None:
        # Cooperative cancel — same discipline as the build worker.
        if self._diff_badge_token != token:
            return
        counts = compute_diff_shortstat(actor)
        if self._diff_badge_token != token or counts is None:
            return
        added, removed = counts
        self.call_from_thread(self._apply_diff_badge, token, added, removed)

    def _apply_diff_badge(self, token: int, added: int, removed: int) -> None:
        """Main-thread commit of badge counts. Stale tokens drop
        silently; the build-path apply will land its own (possibly
        higher, if untracked files contributed) counts later either
        way. Both paths funnel through `_apply_diff_counts` — last
        write wins, which is fine because the build's number is
        authoritative and arrives last."""
        if token != self._diff_badge_token:
            return
        target = self._diff_badge_target_actor
        if target is None:
            return
        self._apply_diff_counts(target, added, removed)

    def _kick_diff_build(self, actor: Actor, force: bool = False) -> None:
        """Start an off-thread diff build with token + 300ms placeholder.

        Skips work entirely when `#diff-scroll` has zero width — that
        means the DIFF tab is hidden (TabbedContent collapses inactive
        panes). The actor is stashed on `_diff_pending_actor` and
        replayed by `_flush_pending_diff_if_visible` once the tab is
        activated.

        Token discipline mirrors the logs path: every kick bumps
        `_diff_build_token`, every worker captures its own token, and
        `_apply_diff_build` only commits when the captured token still
        matches. Stale workers drop their output silently.

        `force=True` skips the cache-key short-circuit in the worker
        — used by the live-refresh poller, which kicks precisely
        BECAUSE the worktree changed even though HEAD didn't move.
        Without this bypass the cache check would early-out and the
        diff would stay stale despite the user editing files."""
        try:
            scroll = self.query_one("#diff-scroll", VerticalScroll)
        except Exception:
            return
        width = scroll.size.width
        if width == 0:
            self._diff_pending_actor = actor.name
            return
        self._diff_pending_actor = None

        self._diff_build_token += 1
        self._diff_build_pending = True
        self._diff_build_target_actor = actor.name
        self._diff_build_target_width = width
        token = self._diff_build_token

        def _maybe_show_placeholder() -> None:
            # Fire only if this build is still the latest one AND
            # hasn't already applied. Any newer kick-off bumps the
            # token; a committed apply clears the pending flag.
            if token != self._diff_build_token or not self._diff_build_pending:
                return
            # Skip the placeholder when we already have content for
            # this actor — wiping fresh content with "Loading diff..."
            # for a few hundred milliseconds is jarring and recurs
            # every 2s while the live poll force-refreshes a slow
            # render. The cache-key actor field tells us whether the
            # currently mounted scroll content matches the actor of
            # this kick; if so, leave it alone until the new build
            # streams or the finalizer commits.
            last_key = self._diff_last_applied_key
            if (
                last_key is not None
                and last_key[0] == self._diff_build_target_actor
            ):
                return
            try:
                scroll = self.query_one("#diff-scroll", VerticalScroll)
            except Exception:
                return
            scroll.remove_children()
            scroll.mount(Static(Text("Loading diff...", style="dim")))

        self.set_timer(0.3, _maybe_show_placeholder)
        self._build_diff_worker(token, actor, width, force)

    @work(thread=True, exclusive=True, group="diff_build")
    def _build_diff_worker(
        self, token: int, actor: Actor, width: int, force: bool = False,
    ) -> None:
        # Cooperative cancel: when a newer build supersedes this one,
        # `_diff_build_token` is already the new value on the main
        # thread. Check at every coarse-grained boundary so we don't
        # keep burning subprocess + render work on output that will be
        # discarded anyway.
        def is_cancelled() -> bool:
            return self._diff_build_token != token

        if is_cancelled():
            return

        head_oid = read_head_oid(actor)
        cache_key = (actor.name, head_oid, width)
        if not force and cache_key == self._diff_last_applied_key:
            # Nothing relevant changed since the last applied build —
            # skip the expensive `compute_diff` + render. Hand control
            # back to the main thread to clear the pending flag.
            # `force=True` (live poll) bypasses this; the worktree
            # has changed even when HEAD hasn't, so the cache key is
            # not a reliable "nothing to redo" signal there.
            self.call_from_thread(self._mark_diff_build_done, token)
            return

        if is_cancelled():
            return

        result = compute_diff(actor)
        if is_cancelled():
            return

        if result.files is None:
            # `compute_diff` reports two flavors of file-less result:
            # benign reasons ("working tree clean", "no repository")
            # which CACHE — repeat kicks at the same key correctly
            # early-out — and "error" which signals a transient
            # internal failure that must NOT be cached, otherwise
            # the user is stuck on the error state until HEAD or
            # width changes. Route the error reason through
            # `_apply_diff_error` (cache-invalidating) and benign
            # reasons through `_apply_diff_text` (cache-promoting).
            if result.reason == "error":
                self.call_from_thread(
                    self._apply_diff_error, token,
                    "Diff error: compute failed",
                )
            else:
                self.call_from_thread(
                    self._apply_diff_text, token, cache_key, result.reason,
                )
            return

        # Stream files into `#diff-scroll` one PrerenderedDiff widget
        # per file as they finish rendering. The first append for this
        # token clears placeholder/prior content on the main thread
        # (see `_diff_append_file`); subsequent appends just mount.
        #
        # Each file's Rich renderable is converted to a list of
        # textual `Strip`s **here in the worker thread** — that's the
        # CPU-bound Segment-generation pass that previously happened
        # on the main thread at paint time and showed up as a UI hang.
        # By the time we call_from_thread, the strips are pre-baked;
        # the main thread's mount + paint becomes effectively
        # constant-time per file.
        is_dark = self.current_theme.dark if self.current_theme else True
        total_added = 0
        total_removed = 0
        try:
            for path, renderable, added, removed in iter_diff_renderables(
                result.files, is_dark, is_cancelled,
            ):
                if is_cancelled():
                    return self._on_stream_cancelled(token)
                strips = renderable_to_strips(renderable, width)
                if is_cancelled():
                    return self._on_stream_cancelled(token)
                self.call_from_thread(
                    self._diff_append_file, token, path, strips,
                )
                total_added += added
                total_removed += removed
        except Exception as e:
            # Mid-stream render error → wipe whatever's mounted and
            # surface the error message in its place. `_apply_diff_error`
            # remove_children's the scroll first, so partial appends
            # don't linger above the error. The error path does NOT
            # promote the cache key, so the next kick at the same
            # (actor, head, width) retries instead of cache-hitting on
            # a stale error mount.
            self.call_from_thread(
                self._apply_diff_error, token, f"Diff error: {e}",
            )
            return
        if is_cancelled():
            return self._on_stream_cancelled(token)
        self.call_from_thread(
            self._apply_diff_build_done,
            token, cache_key, total_added, total_removed,
        )

    def _on_stream_cancelled(self, token: int) -> None:
        """Worker bailout when cancellation flips True mid-stream.

        Partial mounts already on screen STAY (clearing on cancel
        would make every fast actor switch flicker). The catch: the
        scroll no longer reflects `_diff_last_applied_key`, so a
        subsequent kick at the same (actor, head, width) must NOT
        cache-hit. We invalidate the key here when this token had
        actually streamed at least one file (i.e. owns the scroll
        content); without that, the next kick's worker takes the
        early-out path and the partial state stays stuck on screen
        forever."""
        if self._diff_streamed_token == token:
            self.call_from_thread(self._invalidate_diff_cache_key)

    def _invalidate_diff_cache_key(self) -> None:
        """Drop the cached `_diff_last_applied_key` so the next
        build can't short-circuit. Runs on the main thread."""
        self._diff_last_applied_key = None

    def _diff_append_file(
        self,
        token: int,
        file_path: str,
        strips: list,
    ) -> None:
        """Streaming mount of a single pre-rendered file onto
        `#diff-scroll`. Stale tokens (newer kick already in flight)
        skip the mount.

        The first append for a given token clears the scroll —
        wiping the 300ms placeholder if it fired and any leftover
        widgets from a prior kick. From there, each subsequent
        append for the same token mounts one more PrerenderedDiff
        at the bottom, so the user sees files appear in order as
        they render. Pending is flipped off on first append too:
        real content is on screen, the placeholder must not fire.

        `strips` is a list of `textual.strip.Strip` produced off-thread
        by `renderable_to_strips` — mounting + painting the widget is
        effectively constant-time, no CPU-bound Rich render at paint."""
        if token != self._diff_build_token:
            return
        try:
            scroll = self.query_one("#diff-scroll", VerticalScroll)
        except Exception:
            return
        if self._diff_streamed_token != token:
            scroll.remove_children()
            self._diff_streamed_token = token
            self._diff_build_pending = False
        scroll.mount(PrerenderedDiff(strips))

    def _apply_diff_build_done(
        self,
        token: int,
        cache_key: tuple,
        total_added: int,
        total_removed: int,
    ) -> None:
        """Finalize a streamed build: lock in the cache key + tab
        label totals, drop the pending flag. Idempotent against
        per-file appends — only the cache key + label promotion need
        to ride this finalizer; the scroll is already populated by
        the streaming `_diff_append_file` calls.

        Bumps `_diff_badge_token` so any in-flight badge worker
        from the same kick can no longer apply: the build's count
        includes untracked files (which shortstat misses), so a
        late-arriving badge would otherwise revise our authoritative
        number downward and visually flicker."""
        if token != self._diff_build_token:
            return
        self._diff_build_pending = False
        self._diff_last_applied_key = cache_key
        self._diff_badge_token += 1
        target = self._diff_build_target_actor
        if target is not None:
            self._apply_diff_counts(target, total_added, total_removed)
        else:
            self._update_diff_tab_label()

    def _apply_diff_text(
        self, token: int, cache_key: tuple, text: str,
    ) -> None:
        """Mount a benign reason text (e.g. "working tree clean",
        "no repository") and CACHE the key. Repeat kicks at the same
        (actor, head, width) early-out via the cache check — the
        text mount is still on screen, so that's correct."""
        if token != self._diff_build_token:
            return
        try:
            scroll = self.query_one("#diff-scroll", VerticalScroll)
        except Exception:
            self._diff_build_pending = False
            return
        scroll.remove_children()
        # `markup=False` — exception messages and reason strings can
        # contain `[brackets]` that Rich would otherwise interpret as
        # markup (e.g. `[red]` colorizing) and either crash or render
        # unexpectedly. We want literal text.
        scroll.mount(Static(text, markup=False))
        self._diff_build_pending = False
        self._diff_last_applied_key = cache_key
        # Same badge-race guard as `_apply_diff_build_done`: an
        # in-flight badge from this kick must not later overwrite
        # the no-changes / no-repo label we just committed.
        self._diff_badge_token += 1
        target = self._diff_build_target_actor
        if target is not None:
            self._apply_diff_counts(target, 0, 0)
        else:
            self._update_diff_tab_label()

    def _apply_diff_error(self, token: int, text: str) -> None:
        """Mount a render-error message and INVALIDATE the cache key.

        Two reasons the cache must drop here:
        1. The scroll now shows error text, not the diff a prior
           successful build's cache_key represented. Leaving the key
           cached would let the next non-force kick early-out via
           `_mark_diff_build_done` and never repaint the diff.
        2. The render error itself may be transient. The user
           shouldn't have to wait for HEAD or width to change before
           a retry succeeds."""
        if token != self._diff_build_token:
            return
        try:
            scroll = self.query_one("#diff-scroll", VerticalScroll)
        except Exception:
            self._diff_build_pending = False
            return
        scroll.remove_children()
        scroll.mount(Static(text, markup=False))
        self._diff_build_pending = False
        self._diff_last_applied_key = None
        # Reset `_diff_loaded_for` so re-selecting the same actor in
        # the tree retries the build instead of short-circuiting at
        # the `_maybe_refresh_diff` actor-already-loaded check. Without
        # this, the user would be stuck on the error message and have
        # to switch actors and back (or wait for a live-poll force
        # refresh) just to retry. With this reset, a simple re-click
        # in the actor tree fires a fresh build.
        self._diff_loaded_for = None
        # Same badge-race guard as `_apply_diff_build_done` and
        # `_apply_diff_text`: a badge worker from this same kick
        # (still in flight with shortstat counts) must not later
        # land "+5 -3" on top of the error message we just mounted
        # — that would split the tab label and the scroll pane into
        # contradictory states.
        self._diff_badge_token += 1
        target = self._diff_build_target_actor
        if target is not None:
            self._apply_diff_counts(target, 0, 0)
        else:
            self._update_diff_tab_label()

    def _mark_diff_build_done(self, token: int) -> None:
        """Cache-hit early-out path: the worker decided no rebuild was
        needed. Clear the pending flag so the placeholder doesn't fire
        and so a subsequent kick can start a new build cleanly."""
        if token == self._diff_build_token:
            self._diff_build_pending = False

    def _poll_diff_badge_for_selected(self) -> None:
        """2s tick: re-kick the cheap shortstat badge for the
        currently-selected actor regardless of tab/status. The DIFF
        tab label and the OVERVIEW branch row's `+N -M` segment both
        read from `_diff_counts_by_actor`, so a constant-cadence
        refresh keeps both surfaces honest even when the user is on
        another tab and the actor isn't RUNNING (e.g. they edited
        files by hand in the worktree).

        `_kick_diff_badge` is `exclusive=True group="diff_badge"`, so
        a tick that lands while a previous worker is still in flight
        cooperatively cancels it — no thundering herd."""
        try:
            actor = self.query_one(ActorTree).selected_actor
        except Exception:
            return
        if actor is None:
            return
        self._kick_diff_badge(actor)

    # -- Live DIFF refresh while a run is in progress ----------------------

    def _poll_diff_for_running(self) -> None:
        """2s tick (set_interval). Picks up worktree changes for the
        currently-selected RUNNING actor and force-refreshes the
        diff so the tab tracks edits without manual interaction.

        Conditions checked synchronously here on the main thread;
        the subprocess-bound signal capture (`shortstat`) is then
        delegated to a worker, which calls back via
        `_evaluate_diff_poll_signals` for the comparison + refresh
        decision.

        When conditions don't hold (no actor / not RUNNING / DIFF
        not the active tab), the recorded baseline resets so a
        later re-entry doesn't think a stale signal "changed"."""
        actor = self._diff_poll_actor()
        if actor is None:
            self._reset_diff_poll_state()
            return
        self._poll_diff_signals_worker(actor)

    def _diff_poll_actor(self) -> Actor | None:
        """The actor whose diff is eligible for live polling, or
        None if any of (selection, RUNNING/INTERACTIVE status,
        DIFF tab active) doesn't hold.

        `Status.INTERACTIVE` is a display-only overlay applied to
        actors with a live interactive session — under the hood
        they're typically still RUNNING, and the user is driving
        them by hand which can absolutely modify the worktree.
        Excluding INTERACTIVE here would silently disable live
        diff refresh for one of the most common use cases (a user
        running an agent interactively while watching its edits)."""
        try:
            tree = self.query_one(ActorTree)
        except Exception:
            return None
        actor = tree.selected_actor
        if actor is None:
            return None
        status = self._prev_statuses.get(actor.name)
        if status not in (Status.RUNNING, Status.INTERACTIVE):
            return None
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return None
        if tabs.active != "diff":
            return None
        return actor

    @work(thread=True, exclusive=True, group="diff_poll")
    def _poll_diff_signals_worker(self, actor: Actor) -> None:
        """Off-main-thread signal capture: stat the index file, run
        shortstat, and count untracked files. All three are cheap
        individually, but each is a subprocess and on the main
        thread that can drop a frame; threading keeps the TUI smooth.

        The untracked count is the third signal because index mtime
        and shortstat between them miss the most common actor edit:
        creating a new file. Without this, an actor that runs `Write`
        on a file the worktree didn't have wouldn't trigger a diff
        refresh."""
        mtime = git_index_mtime(actor)
        shortstat = compute_diff_shortstat(actor)
        untracked = git_untracked_count(actor)
        self.call_from_thread(
            self._evaluate_diff_poll_signals,
            actor.name, mtime, shortstat, untracked,
        )

    def _evaluate_diff_poll_signals(
        self,
        actor_name: str,
        mtime: float | None,
        shortstat: tuple[int, int] | None,
        untracked: int | None,
    ) -> None:
        """Main-thread signal comparison + refresh decision.

        Re-checks polling conditions because the worker may have
        raced with a tab/actor change since the kick: stale signals
        from a prior selection must not refresh the diff for a
        different actor.

        Re-baselines whenever the actor changes — without that, the
        baseline from alice's signals would compare against bob's on
        the next tick after a switch, falsely "detect a change", and
        fire a redundant force-refresh on top of the actor-switch
        kick `on_tree_node_highlighted` already triggered."""
        actor = self._diff_poll_actor()
        if actor is None or actor.name != actor_name:
            return
        if (
            not self._diff_poll_initialized
            or self._diff_poll_last_actor != actor_name
        ):
            # First observation since conditions became true OR the
            # selected actor changed. The user has just landed on
            # this actor's DIFF, so the active build / cache key is
            # presumably already correct — we don't refresh, just
            # record the baseline for next time.
            self._diff_poll_initialized = True
            self._diff_poll_last_actor = actor_name
            self._diff_poll_last_index_mtime = mtime
            self._diff_poll_last_shortstat = shortstat
            self._diff_poll_last_untracked = untracked
            return
        changed = (
            mtime != self._diff_poll_last_index_mtime
            or shortstat != self._diff_poll_last_shortstat
            or untracked != self._diff_poll_last_untracked
        )
        self._diff_poll_last_index_mtime = mtime
        self._diff_poll_last_shortstat = shortstat
        self._diff_poll_last_untracked = untracked
        if changed:
            self._maybe_refresh_diff(force=True)

    def _reset_diff_poll_state(self) -> None:
        self._diff_poll_initialized = False
        self._diff_poll_last_actor = None
        self._diff_poll_last_index_mtime = None
        self._diff_poll_last_shortstat = None
        self._diff_poll_last_untracked = None

    def _update_diff_tab_label(
        self, added: int | None = None, removed: int | None = None,
    ) -> None:
        """Compose the DIFF tab base label. Plain text (no pill
        colors) — the colored display now lives only in the OVERVIEW
        branch row, where Textual's active-tab reverse-video doesn't
        fight us.

        `added`/`removed` are passed by apply paths that already know
        the counts (badge / build finalizer). When omitted, the label
        is composed from `_diff_counts_by_actor` for the currently
        selected actor — used by polling and clear paths that don't
        carry counts."""
        if added is None or removed is None:
            added = removed = 0
            try:
                actor = self.query_one(ActorTree).selected_actor
            except Exception:
                actor = None
            if actor is not None:
                counts = self._diff_counts_by_actor.get(actor.name)
                if counts is not None:
                    added, removed = counts
        bits: list[str] = []
        if added:
            bits.append(f"+{added}")
        if removed:
            bits.append(f"-{removed}")
        label = "DIFF" if not bits else f"DIFF [{' '.join(bits)}]"
        self._tab_base_labels["diff"] = label
        self._refresh_tab_arrows()

    def _apply_diff_counts(
        self, actor_name: str, added: int, removed: int,
    ) -> None:
        """Store the latest (added, removed) for an actor and refresh
        the two surfaces that read from the cache: the DIFF tab label
        and the OVERVIEW branch row. Skips the overview re-render
        when the actor isn't the one currently shown — the next
        selection / re-render naturally picks up the new value.

        Token discipline in the callers ensures apply only fires for
        the most recently kicked actor (which is the selected one),
        so the tab label always reflects what the user is looking at."""
        prev = self._diff_counts_by_actor.get(actor_name)
        new_counts = (added, removed)
        if prev == new_counts:
            return
        self._diff_counts_by_actor[actor_name] = new_counts
        self._update_diff_tab_label(added, removed)
        current = self._overview_header_actor
        if current is not None and current.name == actor_name:
            status = self._prev_statuses.get(current.name, Status.IDLE)
            self._render_overview_header(current, status)

    def _refresh_tab_arrows(self) -> None:
        """Prefix the currently-active tab with `→` while any
        descendant of the detail panel has focus (tabs bar, the
        active tab's content, etc.). Matches the focus-gated
        reverse-video override in CSS. Safe to call before compose
        finishes.

        Also re-derives the INTERACTIVE label's two-state shape so
        it reflects the live focus state without callers having to
        update `_tab_base_labels` themselves:
          - terminal focused → "INTERACTIVE [CTRL+Z TO EXIT]"
          - terminal blurred or tab inactive → plain "INTERACTIVE"
        """
        try:
            tabbed = self.query_one("#tabs", TabbedContent)
            detail = self.query_one("#detail-panel")
        except Exception:
            return
        focused = detail.has_focus_within
        active_id = tabbed.active
        # Recompute the dynamic INTERACTIVE label up front so the loop
        # below applies the freshest version. Done here rather than in
        # a separate watcher because every event that should re-derive
        # the label (focus change, tab activation, session
        # add/remove) already routes through `_refresh_tab_arrows`.
        self._tab_base_labels["interactive"] = self._interactive_tab_label()
        for tab_id, base in self._tab_base_labels.items():
            try:
                tab = tabbed.get_tab(tab_id)
            except Exception:
                continue  # e.g. interactive tab isn't mounted right now
            if tab is None:
                continue
            if tab_id == active_id and focused:
                # Drop styles when this tab is the focused-active one —
                # Textual paints its own reverse-video focus background
                # over the label, and our pill colors fight it (e.g.
                # green-on-darkgreen badge becomes unreadable on the
                # active-tab background). Use the plain text form.
                plain = base.plain if isinstance(base, Text) else base
                tab.label = f"→ {plain}"
            else:
                tab.label = base

    def _interactive_tab_label(self) -> str:
        """Compose the INTERACTIVE tab label based on terminal focus
        state. See `_refresh_tab_arrows` for the two-state semantics.

        Brackets are escaped with a leading `\\` because Textual's
        Content runs the label string through Rich-style markup
        parsing — `[CTRL+Z TO EXIT]` looks like a style tag with
        attributes (uppercase identifier + space) and gets stripped
        wholesale. The `\\[` escape survives the parse and renders
        as a literal `[`. (`[+N -M]` on the DIFF tab survives without
        escaping because `+` isn't a valid style-name character.)"""
        from .interactive.widget import TerminalWidget
        try:
            tw = self.query_one(TerminalWidget)
        except Exception:
            tw = None
        if tw is not None and tw.has_focus_within:
            return r"INTERACTIVE \[CTRL+Z TO EXIT]"
        return "INTERACTIVE"

    def _remember_detail_tab(self, tab_id: str | None) -> None:
        if tab_id in {"interactive", "info", "diff"}:
            self._preferred_detail_tab = tab_id

    def on_tabbed_content_tab_activated(self, event) -> None:
        # TabbedContent fires TabActivated on mount for the default
        # tab; suppress the focus-push until we're fully ready so the
        # tree (not the default tab's content) carries the initial
        # focus. After ready, push focus into the active tab's
        # content widget so the user can immediately scroll /
        # interact, and so our #detail-panel:focus-within highlight
        # fires on mouse clicks too.
        if self._tabs_ready:
            try:
                tabbed = self.query_one("#tabs", TabbedContent)
            except Exception:
                tabbed = None
            if tabbed is not None and tabbed.active != "interactive":
                self._pending_interactive_focus = None
            if tabbed is not None:
                if self._skip_next_detail_preference_update:
                    self._skip_next_detail_preference_update = False
                else:
                    self._remember_detail_tab(tabbed.active)

        if self._tabs_ready and not self._tree_has_focus():
            pending = self._pending_interactive_focus
            if pending is not None:
                try:
                    if tabbed is not None and tabbed.active == "interactive":
                        self._schedule_focus_interactive_terminal(pending)
                    else:
                        self._focus_detail_content()
                except Exception:
                    self._focus_detail_content()
            else:
                self._focus_detail_content()
        self._refresh_tab_arrows()
        # LIVE just became visible. Its RichLog was width-0 while
        # hidden and we skipped render passes — flush now so the
        # first paint sees real content at the real width instead
        # of a stale/empty cache that then gets corrected.
        self._flush_pending_logs_if_visible()
        # Same story for DIFF: kicks that happened while the tab was
        # hidden stash on `_diff_pending_actor`; flush them now.
        self._flush_pending_diff_if_visible()

    def _flush_pending_logs_if_visible(self) -> None:
        """Re-run the logs renderer once LIVE has a non-zero width,
        using whatever entries were stashed while hidden. Call after
        layout can assign the RichLog its real width (post
        TabActivated + post call_after_refresh is a safe time).

        `_set_logs`'s own skip shortcut handles the common "user
        switched away and back, nothing changed" case — we just
        replay; it returns early when the committed state already
        matches the stashed state."""
        if not self._pending_log_entries or self._pending_log_actor is None:
            return
        actor_name = self._pending_log_actor
        entries = self._pending_log_entries
        def _attempt() -> None:
            try:
                log = self.query_one("#logs-content", RichLog)
            except Exception:
                return
            if log.size.width == 0:
                return
            self._set_logs(actor_name, entries)
        self.call_after_refresh(_attempt)

    @on(events.Click, "#tabs Tabs, #tabs Tab")
    def _on_tabs_click(self, event: events.Click) -> None:
        # Any click landing inside the Tabs bar — whether on a
        # different tab (triggering TabActivated) or on the
        # already-active tab (which wouldn't) — should end with focus
        # on the active tab's content, matching the keyboard path.
        def _focus_after_click() -> None:
            try:
                tabbed = self.query_one("#tabs", TabbedContent)
            except Exception:
                tabbed = None
            if tabbed is not None and tabbed.active != "interactive":
                self._pending_interactive_focus = None
            if tabbed is not None:
                self._remember_detail_tab(tabbed.active)
            self._focus_detail_content()

        self.call_after_refresh(_focus_after_click)

    async def on_key(self, event: events.Key) -> None:
        if (
            event.key != "enter"
            or isinstance(self.focused, (ActorTree, TerminalWidget))
        ):
            return
        try:
            tabbed = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        if tabbed.active != "interactive":
            return
        event.stop()
        event.prevent_default()
        self.action_enter_interactive()

    def on_descendant_focus(self, event) -> None:
        self._refresh_tab_arrows()

    def on_descendant_blur(self, event) -> None:
        self._refresh_tab_arrows()

    def _on_focused_changed(self, focused) -> None:
        self._refresh_focus_indicators()

    def _refresh_focus_indicators(self) -> None:
        self._refresh_tab_arrows()
        try:
            tree = self.query_one(ActorTree)
            tree.set_class(self.focused is tree, "-focus-active")
            tree.refresh()
        except Exception:
            pass
        try:
            from textual.widgets import Tabs
            detail = self.query_one("#detail-panel")
            tabbed = self.query_one("#tabs", TabbedContent)
            detail.set_class(detail.has_focus_within, "-focus-active")
            tabbed.query_one(Tabs).refresh()
            tabbed.refresh()
        except Exception:
            pass

    def _flush_pending_diff_if_visible(self) -> None:
        """Re-kick the stashed diff build once DIFF has a non-zero
        width, mirroring `_flush_pending_logs_if_visible`. Called from
        the tab-activated handler.

        `_kick_diff_build`'s width-0 guard handles the "still hidden"
        case (it just re-stashes), and the cache-key comparison in
        the worker short-circuits the "user toggled tabs but nothing
        else changed" case so we don't pay for redundant rebuilds."""
        if self._diff_pending_actor is None:
            return
        name = self._diff_pending_actor

        def _attempt() -> None:
            try:
                scroll = self.query_one("#diff-scroll", VerticalScroll)
            except Exception:
                return
            if scroll.size.width == 0:
                return
            actor = next(
                (a for a in self._current_actors if a.name == name), None,
            )
            if actor is None:
                # Actor was discarded between the stash and the
                # flush. Clear the stash so subsequent tab
                # activations don't repeatedly try (and fail) to
                # resolve a stale name.
                self._diff_pending_actor = None
                return
            self._kick_diff_build(actor)

        self.call_after_refresh(_attempt)

    # -- Runs ----------------------------------------------------------------

    _RUNS_TABLE_LIMIT = 50

    def _refresh_runs(self, actor: Actor) -> None:
        from datetime import datetime, timezone
        from rich.text import Text
        from ..types import _parse_iso

        with Database.open(_db_path()) as db:
            runs, total = db.list_runs(actor.name, limit=self._RUNS_TABLE_LIMIT)

        # Bake the run count into the section header — styled to
        # match the screenshot's `RUNS · N` heading.
        from rich.text import Text as _Text
        colors = self._overview_palette()
        try:
            label_widget = self.query_one("#overview-runs-label", Static)
            heading = _Text()
            heading.append("RUNS", style=f"bold {colors['primary']}")
            if total == 0:
                pass
            elif total <= self._RUNS_TABLE_LIMIT:
                heading.append(f"  ·  {total}", style="dim")
            else:
                heading.append(
                    f"  ·  showing {self._RUNS_TABLE_LIMIT} of {total}",
                    style="dim",
                )
            label_widget.update(heading)
        except Exception:
            pass

        # Use a Rich Table mounted into a Static so we get true
        # full-width expansion via column ratios. Textual's DataTable
        # auto-sizes columns to content even when the widget is
        # `width: 1fr`, leaving empty space on the right; Rich Table
        # with `expand=True` + `ratio=1` on the Prompt column
        # stretches that column to consume the remaining width.
        from rich import box as rich_box
        from rich.table import Table as RichTable
        rich_table = RichTable(
            show_header=True,
            header_style="dim bold",
            expand=True,
            # SIMPLE_HEAVY adds a single rule under the header and
            # nothing else — matches the mockup's table treatment.
            box=rich_box.SIMPLE_HEAVY,
            border_style="dim",
            padding=(0, 2),
            show_edge=False,
        )
        rich_table.add_column("", no_wrap=True, width=1)
        rich_table.add_column("Prompt", ratio=1, no_wrap=True, overflow="ellipsis")
        rich_table.add_column("Age", no_wrap=True, width=12)
        rich_table.add_column("Duration", no_wrap=True, width=14)

        now = datetime.now(timezone.utc)
        for run in reversed(runs):
            start = _parse_iso(run.started_at) if run.started_at else None
            end = _parse_iso(run.finished_at) if run.finished_at else None
            if start and end:
                secs = int((end - start).total_seconds())
                duration_text = Text(self._humanize_seconds(secs))
            elif start and run.status == Status.RUNNING:
                # In-flight: live duration ticking from started_at.
                secs = int((now - start).total_seconds())
                palette = self._overview_palette()
                duration_text = Text(
                    f"{self._humanize_seconds(secs)} (running)",
                    style=palette["primary"],
                )
            else:
                duration_text = Text("—", style="dim")

            age_text = (
                Text(self._format_relative(run.started_at))
                if run.started_at else Text("—", style="dim")
            )

            # No truncation: the column has overflow="ellipsis" so
            # Rich handles the cut at the actual rendered width.
            prompt_text = Text(run.prompt, no_wrap=True, overflow="ellipsis")

            glyph_text = self._run_status_glyph(run.status, run.exit_code)

            rich_table.add_row(glyph_text, prompt_text, age_text, duration_text)

        try:
            self.query_one("#runs-table", Static).update(rich_table)
        except Exception:
            pass

    def _run_status_glyph(self, status: Status, exit_code: int | None):
        """One-char status indicator for the runs table. Colors track
        the same palette the header pill and activity sparkline use,
        so the three views read consistently."""
        from rich.text import Text
        palette = self._overview_palette()
        if status == Status.RUNNING:
            return Text("●", style=f"bold {palette['primary']}")
        if status == Status.DONE:
            return Text("✓", style=palette["success"])
        if status == Status.ERROR:
            return Text("✕", style=palette["error"])
        return Text("·", style="dim")

    # -- Actions -------------------------------------------------------------

    def on_tree_node_selected(self, event) -> None:
        if event.control is not self.query_one(ActorTree):
            return
        node = event.node
        event.stop()
        if node.children and not node.is_expanded:
            node.expand()
            return
        if node.data is not None:
            self.action_enter_interactive()

    def on_tree_node_highlighted(self, event) -> None:
        # The tree may already be torn down by the time a queued
        # highlight event is dispatched (e.g. on app shutdown after a
        # quick keystroke burst). Bail rather than crash.
        from textual.css.query import NoMatches
        try:
            tree = self.query_one(ActorTree)
        except NoMatches:
            return
        prefer_interactive = not tree.consume_mouse_highlight()
        self._refresh_detail()
        self._maybe_refresh_diff()
        self._sync_detail_view(prefer_interactive=prefer_interactive)

    # -- Interactive mode ----------------------------------------------------

    def _apply_interactive_tab_preference(
        self,
        tabs: TabbedContent,
        previous_active: str,
        *,
        prefer_interactive: bool,
    ) -> None:
        if previous_active == "interactive":
            target = "interactive"
        elif previous_active == "diff":
            target = "diff"
        elif self._preferred_detail_tab == "interactive":
            target = "interactive"
        elif self._preferred_detail_tab == "diff":
            target = "diff"
        elif prefer_interactive:
            target = "interactive"
        else:
            target = "info"
        tabs.active = target
        self._remember_detail_tab(target)

    def _replace_interactive_pane_content(
        self,
        tabs: TabbedContent,
        pane: TabPane,
        *,
        actor_name: str,
        target_widget,
        previous_active: str,
        prefer_interactive: bool,
        token: int,
    ) -> None:
        async def _replace() -> None:
            try:
                current_actor = self.query_one(ActorTree).selected_actor
                current_info = (
                    self._interactive.get(actor_name)
                    if current_actor is not None and current_actor.name == actor_name
                    else None
                )
                if (
                    self._interactive_pane_token != token
                    or current_info is None
                    or current_info.widget is not target_widget
                    or not pane.is_mounted
                ):
                    return

                stale_children = [
                    child for child in pane.children
                    if child is not target_widget
                ]
                if stale_children:
                    await pane.remove_children(stale_children)
                if self._interactive_pane_token != token:
                    return

                try:
                    parent = target_widget.parent
                except Exception:
                    parent = None
                if getattr(target_widget, "is_mounted", False) and parent is not pane:
                    await target_widget.remove()
                if self._interactive_pane_token != token:
                    return

                if target_widget not in list(pane.children):
                    await pane.mount(target_widget)
                if self._interactive_pane_token != token:
                    return

                current_actor = self.query_one(ActorTree).selected_actor
                current_info = (
                    self._interactive.get(actor_name)
                    if current_actor is not None and current_actor.name == actor_name
                    else None
                )
                if current_info is None or current_info.widget is not target_widget:
                    return

                self._interactive_active = actor_name
                self._apply_interactive_tab_preference(
                    tabs,
                    previous_active,
                    prefer_interactive=prefer_interactive,
                )
                self._refresh_focus_indicators()
            except Exception as exc:
                self.notify(
                    f"failed to switch interactive tab: {exc}",
                    severity="error",
                )

        self.run_worker(
            _replace,
            name="replace-interactive-pane",
            group="interactive-pane",
            exclusive=True,
            exit_on_error=False,
        )

    def _sync_detail_view(self, *, prefer_interactive: bool = True) -> None:
        """Add/remove the Interactive tab based on whether the selected
        actor has a live session. When an actor has interactive output,
        prefer that tab over OVERVIEW; preserve DIFF because selecting
        actors while reviewing changes should keep the diff surface."""
        from textual.css.query import NoMatches

        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except NoMatches:
            return
        previous_active = tabs.active
        actor = self.query_one(ActorTree).selected_actor
        info = (
            self._interactive.get(actor.name) if actor is not None else None
        )
        self._interactive_pane_token += 1
        token = self._interactive_pane_token

        existing: TabPane | None = None
        try:
            existing = tabs.get_pane("interactive")
        except Exception:
            existing = None

        if info is None:
            self._pending_interactive_focus = None
            if existing is not None:
                if previous_active == "interactive":
                    self._remember_detail_tab("interactive")
                    self._skip_next_detail_preference_update = True
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
                self._apply_interactive_tab_preference(
                    tabs,
                    previous_active,
                    prefer_interactive=prefer_interactive,
                )
                return
            self._replace_interactive_pane_content(
                tabs,
                existing,
                actor_name=actor.name,
                target_widget=info.widget,
                previous_active=previous_active,
                prefer_interactive=prefer_interactive,
                token=token,
            )
            return

        # add_pane is async — it schedules the mount but we can proceed.
        # Initial label is plain "INTERACTIVE"; `_refresh_tab_arrows`
        # immediately replaces it with the focus-aware variant.
        new_pane = TabPane("INTERACTIVE", info.widget, id="interactive")
        # Insert as the FIRST tab so the order is INTERACTIVE / OVERVIEW
        # / DIFF — INTERACTIVE is the active surface while a session is
        # live and belongs leftmost so the user's eye lands on it
        # immediately. The label hints at the exit key since there's no
        # other affordance for leaving the embedded terminal.
        tabs.add_pane(new_pane, before="info")
        self._interactive_active = actor.name
        self._apply_interactive_tab_preference(
            tabs,
            previous_active,
            prefer_interactive=prefer_interactive,
        )

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
        self._remember_detail_tab("interactive")

        target_widget = info.widget
        self._pending_interactive_focus = target_widget
        self._schedule_focus_interactive_terminal(target_widget)

    def _schedule_focus_interactive_terminal(self, target_widget, attempts: int = 8) -> None:
        def _activate(remaining: int) -> None:
            if self._pending_interactive_focus is not target_widget:
                return
            if not getattr(target_widget, "is_mounted", False):
                if remaining > 0:
                    self.set_timer(0.02, lambda: _activate(remaining - 1))
                return
            target_widget.can_focus = True
            try:
                self.set_focus(target_widget, scroll_visible=False)
            except Exception:
                return
            try:
                target_widget.scroll_end(animate=False, force=True)
            except Exception:
                pass
            self._refresh_focus_indicators()
            if self.focused is target_widget:
                if self._pending_interactive_focus is target_widget:
                    self._pending_interactive_focus = None
                return
            if remaining > 0:
                self.set_timer(0.02, lambda: _activate(remaining - 1))
            elif self._pending_interactive_focus is target_widget:
                self._pending_interactive_focus = None

        self.set_timer(0.02, lambda: _activate(attempts))

    def on_terminal_widget_exit_requested(self, message: TerminalWidget.ExitRequested) -> None:
        """Ctrl+Z from the embedded terminal: keep the INTERACTIVE tab
        active and blur the terminal by moving focus onto the tab bar
        (TabbedContent's inner `Tabs` widget). `TabbedContent` itself
        is not focusable (`can_focus=False`) — focusing it is a
        no-op, which is why earlier attempts appeared to do nothing.
        The `Tabs` widget IS focusable; focusing it lets left/right
        cycle through tabs natively, while the descendant-blur on the
        terminal triggers `_refresh_tab_arrows` to flip the INTERACTIVE
        label out of "[CTRL+Z TO EXIT]" mode."""
        self._pending_interactive_focus = None
        if not self._focus_tabs_bar():
            self.set_focus(None, scroll_visible=False)
            self._refresh_focus_indicators()

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
        # Switch to OVERVIEW BEFORE removing the INTERACTIVE pane.
        # `TabbedContent.remove_pane` on the currently-active tab
        # auto-falls-through to the next pane in tab order — which
        # is DIFF — so without this preemptive switch the user
        # quitting an interactive session would land on DIFF instead
        # of the OVERVIEW where they came from.
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            if tabs.active == "interactive":
                self._remember_detail_tab("info")
                tabs.active = "info"
        except Exception:
            pass
        # _sync_detail_view swaps the Logs-tab content back to RichLog
        # now that the session is gone from the registry.
        self._refresh_detail()
        self._sync_detail_view()
        # The terminal widget just unmounted — drop focus onto the tree
        # so the user can immediately navigate to another actor.
        try:
            self.set_focus(None)
        except Exception:
            return
        def _focus_tree() -> None:
            try:
                self.query_one(ActorTree).focus()
            except Exception:
                pass
        self.call_after_refresh(_focus_tree)

    def get_system_commands(self, screen):
        """Filter Textual's default system-command list and add
        actor-targeted entries for the currently selected actor.

        Skipped: `Maximize` / `Minimize` (we never single-pane an
        actor widget — the layout already gives each piece its own
        space) and `Screenshot` (the SVG-dump is a debug aid
        irrelevant to actor-running).

        Added: `Stop actor` (only when the selected actor is RUNNING)
        and `Discard actor` (always, when something is selected).
        Both target the tree's currently-selected actor — there's no
        per-actor entry in the palette because the user already has
        the actor list as their selection surface, and "Stop alice"
        / "Stop bob" / etc. would clutter the search."""
        skip = {"Maximize", "Minimize", "Screenshot"}
        for command in super().get_system_commands(screen):
            if command.title in skip:
                continue
            yield command

        try:
            selected = self.query_one(ActorTree).selected_actor
        except Exception:
            selected = None
        if selected is None:
            return
        status = self._prev_statuses.get(selected.name, Status.IDLE)
        if status == Status.RUNNING:
            yield SystemCommand(
                "Stop actor",
                f"Stop the running session for {selected.name}",
                lambda name=selected.name: self._palette_stop(name),
            )
        yield SystemCommand(
            "Discard actor",
            f"Delete {selected.name}",
            lambda name=selected.name: self._palette_discard(name),
        )

    def action_show_help_panel(self) -> None:
        """Override Textual's default side-docked HelpPanel with a
        centred modal overlay that mirrors the command palette's
        dim-backdrop feel. The system-command "Keys" entry still
        invokes this method, so the user reaches the overlay via the
        same `p` → "Keys" path."""
        # Already showing? No-op so a double-trigger doesn't stack
        # overlays.
        if any(isinstance(s, HelpOverlay) for s in self.screen_stack):
            return
        self.push_screen(HelpOverlay())

    def action_hide_help_panel(self) -> None:
        """Pair to `action_show_help_panel`. The default Textual
        action queries the active screen for a `HelpPanel` widget;
        ours pops the modal screen we pushed."""
        for screen in reversed(self.screen_stack):
            if isinstance(screen, HelpOverlay):
                screen.dismiss()
                return

    def _palette_stop(self, name: str) -> None:
        """Palette command target: stop the named actor, after a
        confirmation dialog. The dialog returns True/False via its
        `dismiss` value; we run the actual stop only on confirm."""
        def after_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._do_stop(name)

        self.push_screen(
            ConfirmDialog(
                title="Stop actor?",
                message=(
                    f"Stop the running session for {name}?\n"
                    "The agent process will be SIGTERMed."
                ),
                confirm_label="Stop",
            ),
            after_confirm,
        )

    def _do_stop(self, name: str) -> None:
        """Actual stop logic — runs `cmd_stop` and refreshes the
        tree. The MCP server's per-run thread-watcher picks up the
        resulting state transition automatically (it's blocked on
        `agent.wait(pid)`)."""
        from ..commands import cmd_stop
        try:
            actor = self._db_handle().get_actor(name)
            agent = _create_agent(actor.agent)
            cmd_stop(self._db_handle(), agent, RealProcessManager(), name=name)
            self.notify(f"Stopped {name}")
        except Exception as e:
            self.notify(f"failed to stop {name}: {e}", severity="error")
            return
        self._poll_actors_async()

    def _palette_discard(self, name: str) -> None:
        """Palette command target: discard the named actor, after a
        confirmation dialog (this is destructive — the actor row,
        run history, and worktree association all go away)."""
        def after_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._do_discard(name)

        self.push_screen(
            ConfirmDialog(
                title="Discard actor?",
                message=(
                    f"Discard {name}?\n"
                    "This stops any running session, deletes the actor's "
                    "row + run history, and runs the on-discard hook. "
                    "The worktree directory is left on disk."
                ),
                # Trashcan glyph (Material Design icon
                # `nf-md-trash_can` / U+F0A7A) prefixed onto the
                # label as a visual hint of destructive intent.
                # Default variant keeps the rest of the dialog calm
                # — the icon alone carries the meaning.
                confirm_label="\U000F0A7A Discard",
                confirm_variant="default",
            ),
            after_confirm,
        )

    def _do_discard(self, name: str) -> None:
        """Actual discard logic — runs `cmd_discard` with
        `force=True` so a failing on-discard hook doesn't block the
        palette-driven cleanup. The MCP server sees the actor row
        disappear and emits a `status="discarded"` channel
        notification automatically."""
        from ..commands import cmd_discard
        from ..config import load_config as _lc
        try:
            cmd_discard(
                self._db_handle(),
                RealProcessManager(),
                name=name,
                app_config=_lc(),
                force=True,
            )
            self.notify(f"Discarded {name}")
        except Exception as e:
            self.notify(f"failed to discard {name}: {e}", severity="error")
            return
        self._poll_actors_async()

    def _db_handle(self) -> Database:
        """Single Database instance per palette command — opens fresh
        rather than caching, since palette commands are one-shot and
        we don't want a long-lived connection here."""
        return Database.open(_db_path())

    def action_dump_diagnostics(self) -> None:
        import sys
        dump = self._diagnostics.format(limit=200)
        print(f"--- terminal diagnostics ({len(self._diagnostics)} events) ---",
              file=sys.stderr)
        print(dump, file=sys.stderr)
        # Pyte history + visible-buffer dump for the currently-active
        # interactive widget. Useful for diagnosing rendering anomalies
        # (e.g. apparent row duplication on scroll-back) where the
        # smoking gun is what's actually in pyte's history vs visible
        # buffer at the moment the issue is on screen.
        try:
            tabbed = self.query_one("#tabs", TabbedContent)
            if tabbed.active == "interactive":
                from .interactive.widget import TerminalWidget
                tw = self.query_one(TerminalWidget)
                ts = tw._screen
                hist = list(ts._screen.history.top)
                print(
                    f"--- pyte history.top ({len(hist)} rows; meaningful "
                    f"size={ts.history_size()}) ---",
                    file=sys.stderr,
                )
                for i, row in enumerate(hist):
                    text = "".join(
                        row.get(x).data if row.get(x) else " "
                        for x in range(ts.cols)
                    ).rstrip()
                    print(f"hist[{i:>3}] {text!r}", file=sys.stderr)
                print(
                    f"--- pyte visible buffer ({ts.rows} rows) ---",
                    file=sys.stderr,
                )
                for y in range(ts.rows):
                    text = "".join(
                        ts._screen.buffer[y][x].data for x in range(ts.cols)
                    ).rstrip()
                    print(f"buf[{y:>3}] {text!r}", file=sys.stderr)
        except Exception as e:
            print(f"--- pyte dump skipped: {e!r} ---", file=sys.stderr)
        print("--- end ---", file=sys.stderr)
        self.notify(f"dumped {len(self._diagnostics)} diagnostic events to stderr")

    def on_unmount(self) -> None:
        """Textual app teardown: kill all live interactive subprocesses so
        no PTY child outlives the watch process. Uses the non-blocking
        shutdown path — a blocking waitpid here stalls Textual's own
        shutdown coroutine and leads to a hang that only Ctrl+C escapes."""
        self._interactive.shutdown()

    def _focus_tabs_bar(self) -> bool:
        """Synchronously focus TabbedContent's inner Tabs widget."""
        from textual.widgets import Tabs
        try:
            tabbed = self.query_one("#tabs", TabbedContent)
            tabs_bar = tabbed.query_one(Tabs)
        except Exception:
            return False
        tabs_bar.can_focus = True
        self.set_focus(tabs_bar, scroll_visible=False)
        self._refresh_focus_indicators()
        return self.focused is tabs_bar

    def _focus_actor_tree(self) -> bool:
        try:
            tree = self.query_one(ActorTree)
        except Exception:
            return False
        self.set_focus(tree, scroll_visible=False)
        self._refresh_focus_indicators()
        return self.focused is tree

    def _focus_detail_content(self, tab_id: str | None = None) -> None:
        if tab_id is None:
            try:
                tabs = self.query_one("#tabs", TabbedContent)
            except Exception:
                return
            tab_id = tabs.active

        if tab_id == "interactive":
            # Don't auto-grab the terminal — that's reserved for `i`
            # / Enter on the tab bar. Land focus on the inner Tabs
            # widget so further left/right cycle through tabs
            # natively (ContentTabs has its own previous_tab /
            # next_tab bindings). Without this, app-level navigation
            # to "interactive" would leave focus on whatever the
            # previous tab had (e.g. #logs-content), which is now
            # behind a hidden TabPane and consumes nothing useful.
            self._focus_tabs_bar()
            return

        focus_map = {
            "diff": "#diff-scroll",
            # OVERVIEW: focus the embedded RichLog so PageUp/Down
            # scroll through the log feed without the user having to
            # click into it. The log lives below the static header
            # card inside `#overview-scroll`.
            "info": "#logs-content",
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
        if tab_id != "interactive":
            self._pending_interactive_focus = None
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = tab_id
        self._remember_detail_tab(tab_id)
        if tab_id == "diff":
            self._maybe_refresh_diff(force=True)
        self._focus_detail_content(tab_id)

    TAB_ORDER = ["info", "diff"]

    def _live_tab_order(self) -> list[str]:
        """Tab cycle order, dynamically prepending `interactive` when
        an interactive pane is currently mounted. Without this, after
        Ctrl+Z the user's left/right arrows fall through the app-level
        navigate_{left,right} actions but the active tab id
        (`interactive`) isn't in the static TAB_ORDER, so the actions
        no-op. ContentTabs's own bindings would handle cycling when
        the tab bar has focus, but the arrow keys come up to the App
        whenever focus lands on a non-Tabs widget that doesn't claim
        them — so the App needs to know the live order too."""
        try:
            tabbed = self.query_one("#tabs", TabbedContent)
            tabbed.get_tab("interactive")
        except Exception:
            return list(self.TAB_ORDER)
        return ["interactive", *self.TAB_ORDER]

    def action_focus_actors(self) -> None:
        self._focus_actor_tree()

    def _tree_has_focus(self) -> bool:
        # Guard against teardown races: queries during the message-pump
        # shutdown can hit a screen stack that's already been popped or
        # a DOM that no longer contains the tree. Either way, "tree
        # has focus" can't be true when there's nothing to focus, so
        # return False rather than letting the exception propagate.
        try:
            return self.focused is self.query_one(ActorTree)
        except Exception:
            return False

    def action_navigate_left(self) -> None:
        if self._tree_has_focus():
            tree = self.query_one(ActorTree)
            node = tree.cursor_node
            if node and node.children and node.is_expanded:
                node.collapse()
            return
        tabs = self.query_one("#tabs", TabbedContent)
        current = tabs.active
        order = self._live_tab_order()
        if current in order:
            idx = order.index(current)
            if idx == 0:
                self._focus_actor_tree()
            else:
                self.action_show_tab(order[idx - 1])

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
            order = self._live_tab_order()
            if current in order:
                idx = order.index(current)
                if idx < len(order) - 1:
                    self.action_show_tab(order[idx + 1])

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
