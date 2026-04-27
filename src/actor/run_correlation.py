"""Map log entries back to the run rows that produced them.

The core primitive: every ``Run`` row carries a byte range
``[log_start_offset, log_end_offset)`` into its agent's session
rollout file. Every ``LogEntry`` parsed from that rollout carries
its ``source_offset``. Correlating the two is arithmetic — no
timestamp heuristics, no dependency on agent-internal fields.

Open runs (``log_end_offset IS NULL``) and pre-feature rows
(``log_start_offset IS NULL``) are handled here so callers only see
a clean ``entries-to-run-id`` mapping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .interfaces import LogEntry
from .types import Run


@dataclass(frozen=True)
class RunRange:
    """Effective byte range a run owns for correlation purposes.

    Derived from ``Run.log_start_offset`` / ``log_end_offset`` plus
    context about neighboring runs: a ``log_end_offset=None`` run
    gets its end from the next run's start, or from the current file
    size if it's the last one. Using this derived range — not the
    raw DB columns — keeps bucketing correct when a run was
    finalized without an explicit end-offset stamp (crash,
    stale-sweep, or a finalize path that ran before this feature).
    """
    run_id: int
    start: int
    end: Optional[int]  # None = open-ended; matches entries >= start


def compute_run_ranges(
    runs: List[Run], current_file_size: Optional[int] = None,
) -> List[RunRange]:
    """Derive effective byte ranges from a list of Run rows.

    ``runs`` should be all runs for one actor (ordered doesn't
    matter; this function sorts them by ``log_start_offset``). Runs
    with ``log_start_offset IS NULL`` are skipped — they predate the
    offset-tracking feature and are not correlatable by this
    mechanism.

    For a run with ``log_end_offset IS NOT NULL`` the effective end
    is that value. For an open run (``NULL``), the effective end is:

    - the next run's ``log_start_offset`` if one exists — covers the
      common case of a stale/crashed run followed by a later run,
    - otherwise ``current_file_size`` if supplied,
    - otherwise ``None`` — entries at any offset ≥ ``start`` match.
    """
    ranked = [r for r in runs if r.log_start_offset is not None]
    ranked.sort(key=lambda r: r.log_start_offset)  # type: ignore[arg-type,return-value]

    ranges: List[RunRange] = []
    for idx, run in enumerate(ranked):
        start = run.log_start_offset  # type: ignore[assignment]
        end: Optional[int] = run.log_end_offset
        if end is None:
            # Derive: the next run's start caps an open bucket.
            next_start: Optional[int] = None
            for later in ranked[idx + 1:]:
                # Skip degenerate pairs where a later run sits
                # exactly at `start` — nothing to attribute to `run`
                # between them anyway; continue to find a strictly
                # greater boundary.
                if later.log_start_offset is not None and later.log_start_offset > start:
                    next_start = later.log_start_offset
                    break
            if next_start is not None:
                end = next_start
            elif current_file_size is not None:
                end = current_file_size
            else:
                end = None  # truly open
        ranges.append(RunRange(run_id=run.id, start=start, end=end))
    return ranges


def bucket_entries_by_run(
    entries: Iterable[LogEntry],
    runs: List[Run],
    current_file_size: Optional[int] = None,
) -> Dict[Optional[int], List[LogEntry]]:
    """Group ``entries`` by the run row that produced them.

    The returned dict is keyed by ``run_id`` (int) for entries that
    correlate to a run, or ``None`` for orphan entries — ones that
    carry no ``source_offset`` (agent that doesn't track offsets, or
    legacy data) or that fall outside every known run range (written
    by a ``claude --resume`` invoked outside actor.sh, for example).

    Ranges are built once via ``compute_run_ranges`` and scanned
    linearly per entry — O(N·M) but both N and M are small per
    actor, so simplicity wins here.
    """
    ranges = compute_run_ranges(runs, current_file_size)
    buckets: Dict[Optional[int], List[LogEntry]] = {}
    for entry in entries:
        bucket_key = _lookup(entry.source_offset, ranges)
        buckets.setdefault(bucket_key, []).append(entry)
    return buckets


def _lookup(offset: Optional[int], ranges: List[RunRange]) -> Optional[int]:
    """Find the run whose effective range contains ``offset``, or None."""
    if offset is None:
        return None
    for r in ranges:
        if offset < r.start:
            continue
        if r.end is None or offset < r.end:
            return r.run_id
    return None
