"""Pure grouping of a parsed ESC/POS op stream into per-cut receipts.

A real printer physically cuts the paper on `GS V` (see `core.ops.CutOp`).
Today's viewer accumulates every op it has ever seen into a single,
ever-growing strip; this module turns that flat stream back into the
separate receipts a person actually printed -- purely from the op list,
no Tk, no I/O, no global state.

Only depends on `core.ops` (read-only, for the `CutOp` vocabulary),
keeping the `render/` -> `core/` dependency direction the same as the
rest of this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple

from core.ops import CutOp


@dataclass(frozen=True)
class Receipt:
    """One printed receipt: the ops between two cuts (or since the last
    cut, for a receipt still in progress), plus display metadata."""

    sequence: int
    timestamp: datetime
    ops: Tuple[object, ...]
    byte_count: Optional[int] = None


def split_into_receipts(
    ops: Sequence[object],
    *,
    start_sequence: int = 1,
    byte_offsets: Optional[Sequence[int]] = None,
    total_bytes: Optional[int] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> List[Receipt]:
    """Group `ops` into `Receipt`s, splitting at each `CutOp` boundary.

    A cut ends the receipt it appears in (the `CutOp` itself is kept as
    the last op of that receipt). Any ops left over after the final cut
    -- or the entire stream, if it contains no cut at all -- form a
    trailing, not-yet-cut receipt.

    `byte_offsets`, if given, must be the same length as `ops`: the
    cumulative number of bytes received by the time each op arrived.
    When supplied, each receipt's `byte_count` is the delta between the
    offset right before its first op and the offset of its last op;
    without it, `byte_count` is left as `None` -- this function only
    knows about ops, never about the raw byte stream that produced
    them.

    `total_bytes`, if given alongside `byte_offsets`, is the total
    number of raw bytes received so far. It can be larger than
    `byte_offsets[-1]` whenever the caller is still buffering a partial
    op it has not finished decoding yet (e.g. a raster image band split
    across more than one network chunk) -- bytes that were physically
    received but do not yet belong to any completed op. When supplied,
    the *trailing* receipt -- the one still open, not yet terminated by
    a cut -- is credited with every byte received so far instead of
    just the bytes accounted for by a completed op, so a chunk that
    completes no new op still shows up in the session history instead
    of silently vanishing from it. A closed receipt (one ending in a
    `CutOp`) is never affected: once a cut has been received, its byte
    count is exact and final. If there are no ops at all yet but
    `total_bytes` is positive, a single placeholder receipt (no ops,
    `byte_count=total_bytes`) is returned instead of an empty list, so
    bytes already received are never silently unrepresented.
    """
    if byte_offsets is not None and len(byte_offsets) != len(ops):
        raise ValueError("byte_offsets must be the same length as ops")

    timestamp_factory = now or datetime.now

    receipts: List[Receipt] = []
    sequence = start_sequence
    current_ops: List[object] = []
    current_start_index = 0

    def close_receipt(end_index: int, *, is_trailing_open_receipt: bool = False) -> None:
        nonlocal sequence, current_ops, current_start_index
        if not current_ops:
            return
        byte_count = None
        if byte_offsets is not None:
            start_offset = byte_offsets[current_start_index - 1] if current_start_index > 0 else 0
            end_offset = byte_offsets[end_index]
            if is_trailing_open_receipt and total_bytes is not None and total_bytes > end_offset:
                end_offset = total_bytes
            byte_count = end_offset - start_offset
        receipts.append(
            Receipt(
                sequence=sequence,
                timestamp=timestamp_factory(),
                ops=tuple(current_ops),
                byte_count=byte_count,
            )
        )
        sequence += 1
        current_ops = []
        current_start_index = end_index + 1

    for index, op in enumerate(ops):
        current_ops.append(op)
        if isinstance(op, CutOp):
            close_receipt(index)

    close_receipt(len(ops) - 1, is_trailing_open_receipt=True)

    if not receipts and total_bytes:
        receipts.append(
            Receipt(sequence=sequence, timestamp=timestamp_factory(), ops=(), byte_count=total_bytes)
        )

    return receipts
