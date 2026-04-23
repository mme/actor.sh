"""Shared helpers for JSONL rollout file readers.

Claude and Codex both persist their session transcripts as newline-
delimited JSON. The streaming read path is identical on both sides:
seek to a byte offset, read to EOF, split on newlines, defer any
unterminated tail until more bytes arrive. Isolate that slicing here
so both agent implementations share one behavior.
"""
from __future__ import annotations

from typing import Optional, Tuple


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
