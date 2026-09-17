"""Comparative code page decoder: an inspection-only side view.

Real integrations get code pages wrong in both directions -- the sender
encodes with one table, the printer is told to use another -- and the
render pipeline's job (see `core.parser`) is to faithfully reproduce
whatever mojibake that mismatch produces, never to "fix" it. This
module answers a different question a human debugging that mismatch
actually needs: "if I try every code page this simulator knows, which
one reads as real prose?" -- by decoding the exact same raw bytes under
every candidate table side by side.

This is pure inspection. It never mutates parser/render state, it is
never consulted by `core.parser` or `render.raster` while producing
what actually gets drawn, and it never imports from `render/` or
`transport/`. `render.viewer` is the only place expected to call into
it, as a read-only side panel (see `Viewer._show_codepage_comparison_window`).
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from core.commands import CODEPAGE_MAP

# The task's explicit minimum candidate set, by CODEPAGE_MAP id:
# 0=CP437, 2=CP850, 3=CP860, 16=CP1252. `decode_under_candidates()`
# defaults to every id in CODEPAGE_MAP, which is a superset of this.
DEFAULT_CANDIDATE_IDS = (0, 2, 3, 16)


def decode_under_candidates(
    raw: bytes, candidate_ids: Optional[Iterable[int]] = None
) -> Dict[int, str]:
    """Decode `raw` under every candidate code page id, side by side.

    `candidate_ids` defaults to every id in `commands.CODEPAGE_MAP`
    (every table this simulator knows how to decode at all -- see
    `core/commands.py` for exactly which ids that is and why). Ids
    outside `CODEPAGE_MAP` are silently skipped rather than raising,
    since this is a "try what we have" comparison, not a validator.

    Uses `errors="replace"` unconditionally: a candidate table that
    cannot represent some byte in `raw` must never crash this
    comparison (that byte simply was not intended for that table,
    which is itself useful information) -- this is the one place in
    the codebase where that is the correct trade-off, because this
    output is never what gets drawn on the simulated receipt.
    """
    ids = list(candidate_ids) if candidate_ids is not None else list(CODEPAGE_MAP)
    return {
        cid: raw.decode(CODEPAGE_MAP[cid], errors="replace")
        for cid in ids
        if cid in CODEPAGE_MAP
    }
