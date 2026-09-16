"""Structured diagnostics emitted by `core.parser.Parser`.

This is a separate concern from logging. `Parser._skip_unknown` keeps
calling `logger.warning(...)` exactly as before (see `core/parser.py`),
so anything watching the log stream still sees the same messages. But
that information never reached the person looking at the viewer window
instead of a console -- which is exactly how a real bug (`FS .`
rendering as visible text) went unnoticed for a while. This module
gives the UI layer a structured, log-level-independent way to see the
same information.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Diagnostic:
    """One thing the parser could not make sense of.

    `offset` is the absolute byte offset in the stream where the
    unrecognized data starts, `raw_bytes` is exactly what was skipped,
    `reason` is a short human-readable explanation, and `severity`
    mirrors the logging level this would have been reported at
    ("warning" today; left open for future distinctions).

    `source` is set by the composition root (`main.py`), never by
    `Parser` itself, to say where a diagnostic came from when that is
    not otherwise obvious -- e.g. "opened file: capture.bin" for a file
    loaded via the Open button while a live transport is still running.
    Diagnostics produced by the live transport leave it `None`, so
    the diagnostics panel can tell the two apart instead of silently
    merging them into one undifferentiated count.
    """

    offset: int
    raw_bytes: bytes
    reason: str
    severity: str = "warning"
    source: Optional[str] = None
