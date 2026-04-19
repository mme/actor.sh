"""Output batcher: coalesce rapid refreshes into at most one per interval.

When a subprocess dumps a lot of output (initial TUI redraw, piping a
large file, etc.) we can get dozens of chunks in a single scheduler tick.
Each chunk parses into the screen buffer fine, but rendering after every
chunk causes visible flicker and wastes CPU.

The batcher is a pure function-with-state: `on_bytes(n)` records that n
bytes arrived. `should_refresh_now(now)` tells the caller whether it's
time to render, considering a minimum inter-refresh interval and a
maximum defer-time so the UI still feels live during sustained output.

No I/O, no threads, no timers — the caller owns the clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RefreshBatcher:
    """Decide when to re-render based on bytes-in + wall time."""

    # Minimum wall time between refreshes (seconds). Tiny outputs still
    # batch, but anything beyond this gap always refreshes.
    min_interval: float = 0.016   # ~60fps ceiling
    # Maximum time to defer a refresh when bytes keep arriving. Guarantees
    # the UI updates even during a never-ending output firehose.
    max_defer: float = 0.050
    # State
    _pending_bytes: int = 0
    _pending_since: Optional[float] = None
    _last_refresh: float = field(default=float("-inf"))

    def on_bytes(self, n: int, now: float) -> None:
        """Note that n bytes arrived at time `now`."""
        if n <= 0:
            return
        self._pending_bytes += n
        if self._pending_since is None:
            self._pending_since = now

    def should_refresh_now(self, now: float) -> bool:
        """True if the caller should re-render at `now`."""
        if self._pending_bytes == 0:
            return False
        # If it's been long enough since the last refresh, fire.
        if now - self._last_refresh >= self.min_interval:
            return True
        # Otherwise wait — but don't wait longer than max_defer.
        since = now - (self._pending_since or now)
        return since >= self.max_defer

    def mark_refreshed(self, now: float) -> int:
        """Caller invokes this after re-rendering. Returns bytes flushed."""
        flushed = self._pending_bytes
        self._pending_bytes = 0
        self._pending_since = None
        self._last_refresh = now
        return flushed

    def pending_bytes(self) -> int:
        return self._pending_bytes
