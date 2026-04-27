"""Shared helpers for JSONL rollout file readers.

Claude and Codex both persist their session transcripts as newline-
delimited JSON. The streaming read path is identical on both sides:
seek to a byte offset, read to EOF, split on newlines, defer any
unterminated tail until more bytes arrive. Isolate that slicing here
so both agent implementations share one behavior.
"""
from __future__ import annotations

from typing import Iterator, List, Optional, Tuple


def split_complete_lines(data: bytes) -> Tuple[str, Optional[int]]:
    """Split a raw byte chunk into the UTF-8 text of its complete
    (newline-terminated) lines, plus the byte offset to advance a
    cursor past the last complete newline.

    Returns ``(text, advance)``:
        - ``text`` — the UTF-8 decoded portion ending at the last
          newline. Empty if no complete line exists.
        - ``advance`` — ``None`` if there's no newline at all (caller
          should NOT advance its cursor; entire chunk is a partial
          line). Otherwise an int: byte count to advance past the
          last complete line.

    Decoding uses ``errors="replace"`` so arbitrary bytes can't crash
    us; newline boundaries are single-byte ASCII so multi-byte UTF-8
    sequences are never split across a line boundary."""
    if b"\n" not in data:
        return "", None
    last_nl = data.rfind(b"\n")
    complete = data[: last_nl + 1]
    return complete.decode("utf-8", errors="replace"), last_nl + 1


def split_complete_lines_with_offsets(
    data: bytes, base_offset: int = 0,
) -> Tuple[List[Tuple[int, str]], Optional[int]]:
    """Same contract as ``split_complete_lines`` but emits one
    ``(absolute_byte_offset, line_text)`` pair per complete line
    instead of a single concatenated string.

    Each ``absolute_byte_offset`` is ``base_offset`` plus the byte
    position of that line's first character within ``data``. Callers
    pass ``base_offset`` equal to the file offset where ``data``
    started so the tuple offsets are absolute in the file — this is
    what lets a reader stamp each downstream ``LogEntry`` with its
    source location for run-id bucketing.

    Blank lines between newlines (e.g. trailing ``\\n\\n``) are
    preserved as empty-text tuples; downstream parsers already skip
    whitespace-only lines before decoding JSON, so we don't filter
    here — keeping the byte accounting simple is worth the trivial
    extra iteration."""
    if b"\n" not in data:
        return [], None
    last_nl = data.rfind(b"\n")
    complete = data[: last_nl + 1]

    lines: List[Tuple[int, str]] = []
    pos = 0
    while pos < len(complete):
        nl = complete.find(b"\n", pos)
        if nl == -1:
            break
        line_bytes = complete[pos:nl]
        lines.append((
            base_offset + pos,
            line_bytes.decode("utf-8", errors="replace"),
        ))
        pos = nl + 1
    return lines, last_nl + 1


def iter_lines_with_offsets(
    data: bytes, base_offset: int = 0,
) -> Iterator[Tuple[int, str]]:
    """Yield every line in ``data`` as ``(absolute_offset, text)``,
    including a final unterminated line. Used by the full-read path
    (``read_logs``) where the entire file is read once and every
    line should be parsed — even if the last line isn't
    newline-terminated, which can happen for in-progress writes or
    files that were never flushed with a trailing newline."""
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        end = len(data) if nl == -1 else nl
        yield (
            base_offset + pos,
            data[pos:end].decode("utf-8", errors="replace"),
        )
        if nl == -1:
            break
        pos = nl + 1
