"""Ring buffer of terminal I/O events for post-mortem of flicker / hangs.

Every layer of the interactive stack can push a TerminalEvent; the ring
buffer keeps the most recent N events. A hidden binding in the watch app
dumps them to stderr so we can see exactly what happened when something
misbehaves in a live session.

Pure: no I/O, no clocks except for a user-injectable `now` callable.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, List, Optional


class EventKind(str, Enum):
    READ = "read"
    WRITE = "write"
    REFRESH = "refresh"
    REFRESH_SKIP = "skip"
    RESIZE = "resize"
    EXIT = "exit"
    ERROR = "error"


@dataclass(frozen=True)
class TerminalEvent:
    t: float
    kind: EventKind
    full_size: int
    preview_bytes: bytes
    note: str


_PREVIEW_BYTES = 32


class DiagnosticRecorder:
    """Thread-safe-ish ring buffer (CPython list append/deque are atomic)."""

    def __init__(self, capacity: int = 1024, now: Callable[[], float] = time.monotonic) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._events: Deque[TerminalEvent] = deque(maxlen=capacity)
        self._now = now

    def record(
        self,
        kind: EventKind,
        data: Optional[bytes] = None,
        note: str = "",
    ) -> None:
        size = len(data) if data is not None else 0
        preview = b"" if data is None else bytes(data[:_PREVIEW_BYTES])
        self._events.append(TerminalEvent(
            t=self._now(),
            kind=kind,
            full_size=size,
            preview_bytes=preview,
            note=note,
        ))

    def recent(self, limit: Optional[int] = None) -> List[TerminalEvent]:
        if limit is None or limit >= len(self._events):
            return list(self._events)
        items = list(self._events)
        return items[-limit:]

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)

    def format(self, limit: Optional[int] = None) -> str:
        lines: List[str] = []
        for ev in self.recent(limit):
            preview = ev.preview_bytes.decode("ascii", errors="replace").replace("\x1b", "\\x1b")
            lines.append(
                f"  t={ev.t:.4f} {ev.kind.value:8s} size={ev.full_size:5d}  {preview!r}  {ev.note}"
            )
        return "\n".join(lines)
