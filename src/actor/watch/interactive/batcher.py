"""Output batcher: coalesce rapid refreshes into at most one per interval.

The caller owns the clock (no I/O, no threads). Under bursty PTY output
we'd otherwise render once per chunk, producing visible flicker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RefreshBatcher:
    # min_interval floors the refresh rate (~60fps); max_defer caps how
    # long we hold back a refresh when bytes keep streaming in.
    min_interval: float = 0.016
    max_defer: float = 0.050
    _pending_bytes: int = 0
    _pending_since: Optional[float] = None
    _last_refresh: float = field(default=float("-inf"))

    def on_bytes(self, n: int, now: float) -> None:
        if n <= 0:
            return
        self._pending_bytes += n
        if self._pending_since is None:
            self._pending_since = now

    def should_refresh_now(self, now: float) -> bool:
        if self._pending_bytes == 0:
            return False
        if now - self._last_refresh >= self.min_interval:
            return True
        since = now - (self._pending_since or now)
        return since >= self.max_defer

    def mark_refreshed(self, now: float) -> int:
        flushed = self._pending_bytes
        self._pending_bytes = 0
        self._pending_since = None
        self._last_refresh = now
        return flushed

    def pending_bytes(self) -> int:
        return self._pending_bytes
