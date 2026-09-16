"""Tests for render.receipts.split_into_receipts: pure grouping of a
parsed op stream into per-cut receipts. No Tk, no I/O.
"""

from datetime import datetime

from core.ops import CutOp, LineFeedOp, TextOp
from core.state import PrinterState
from render.receipts import Receipt, split_into_receipts


def _text(s: str) -> TextOp:
    return TextOp(text=s, style=PrinterState())


def test_empty_input_yields_no_receipts():
    assert split_into_receipts([]) == []


def test_ops_with_no_cut_yield_one_trailing_receipt():
    ops = [_text("HELLO"), LineFeedOp()]

    receipts = split_into_receipts(ops)

    assert len(receipts) == 1
    assert receipts[0].ops == tuple(ops)
    assert receipts[0].sequence == 1


def test_ops_with_one_cut_yield_a_single_receipt_ending_in_the_cut():
    ops = [_text("HELLO"), LineFeedOp(), CutOp(mode="full")]

    receipts = split_into_receipts(ops)

    assert len(receipts) == 1
    assert receipts[0].ops == tuple(ops)


def test_trailing_ops_after_the_last_cut_form_their_own_receipt():
    ops = [_text("A"), CutOp(mode="full"), _text("B"), LineFeedOp()]

    receipts = split_into_receipts(ops)

    assert len(receipts) == 2
    assert receipts[0].ops == (ops[0], ops[1])
    assert receipts[1].ops == (ops[2], ops[3])
    assert receipts[0].sequence == 1
    assert receipts[1].sequence == 2


def test_consecutive_cuts_yield_separate_receipts():
    ops = [_text("A"), CutOp(mode="full"), CutOp(mode="partial")]

    receipts = split_into_receipts(ops)

    assert len(receipts) == 2
    assert receipts[0].ops == (ops[0], ops[1])
    assert receipts[1].ops == (ops[2],)


def test_sequence_numbers_can_start_at_an_arbitrary_offset():
    ops = [CutOp(mode="full"), CutOp(mode="full")]

    receipts = split_into_receipts(ops, start_sequence=5)

    assert [r.sequence for r in receipts] == [5, 6]


def test_each_receipt_gets_a_timestamp_from_the_supplied_factory():
    ops = [CutOp(mode="full"), _text("A")]
    ticks = iter([datetime(2026, 1, 1, 12, 0, 0), datetime(2026, 1, 1, 12, 0, 1)])

    receipts = split_into_receipts(ops, now=lambda: next(ticks))

    assert receipts[0].timestamp == datetime(2026, 1, 1, 12, 0, 0)
    assert receipts[1].timestamp == datetime(2026, 1, 1, 12, 0, 1)


def test_byte_count_is_none_when_offsets_are_not_supplied():
    ops = [_text("A"), CutOp(mode="full")]

    receipts = split_into_receipts(ops)

    assert receipts[0].byte_count is None


def test_byte_count_is_derived_from_supplied_cumulative_byte_offsets():
    ops = [_text("A"), CutOp(mode="full"), _text("B")]
    # cumulative bytes received *after* each op arrived
    offsets = [10, 13, 20]

    receipts = split_into_receipts(ops, byte_offsets=offsets)

    assert receipts[0].byte_count == 13  # 0 -> 13
    assert receipts[1].byte_count == 7  # 13 -> 20 (still open, no cut yet)


def test_byte_offsets_length_mismatch_raises_value_error():
    ops = [_text("A"), CutOp(mode="full")]

    try:
        split_into_receipts(ops, byte_offsets=[1])
        assert False, "expected a ValueError"
    except ValueError:
        pass


def test_receipt_is_frozen():
    receipt = Receipt(sequence=1, timestamp=datetime.now(), ops=(), byte_count=None)
    try:
        receipt.sequence = 2
        assert False, "Receipt must be immutable"
    except Exception:
        pass


# -- total_bytes: honest accounting for a receipt still open, or not yet
# started, while the caller is buffering a partial op -----------------


def test_total_bytes_tops_up_the_trailing_open_receipt_past_its_last_known_op_offset():
    # The caller has received 50 bytes total, but only 20 of them belong
    # to a completed op (the rest are still buffered as part of an op
    # the parser has not finished decoding yet, e.g. a raster image band
    # split across more than one network chunk). The trailing, not-yet-
    # cut receipt must be credited with all 50 bytes, not just 20.
    ops = [_text("A")]
    receipts = split_into_receipts(ops, byte_offsets=[20], total_bytes=50)

    assert len(receipts) == 1
    assert receipts[0].byte_count == 50


def test_total_bytes_does_not_affect_a_receipt_already_closed_by_a_cut():
    ops = [_text("A"), CutOp(mode="full")]
    # total_bytes claims more bytes arrived after the cut too, but a
    # closed receipt's byte count is final -- those extra bytes belong
    # to whatever receipt comes after, not this one.
    receipts = split_into_receipts(ops, byte_offsets=[10, 13], total_bytes=50)

    assert len(receipts) == 1
    assert receipts[0].byte_count == 13


def test_total_bytes_only_tops_up_when_it_exceeds_the_last_known_offset():
    ops = [_text("A")]
    # total_bytes equal to (or, defensively, less than) the last known
    # op offset must never shrink the byte count.
    receipts = split_into_receipts(ops, byte_offsets=[20], total_bytes=20)

    assert receipts[0].byte_count == 20


def test_total_bytes_yields_a_placeholder_receipt_when_no_op_has_completed_yet():
    # Bytes have been received (e.g. the first chunk of a print landed
    # entirely inside a still-incomplete command), but the parser has
    # not produced a single op yet. Those bytes must still be
    # represented somewhere instead of silently vanishing until the
    # first op finally completes.
    receipts = split_into_receipts([], byte_offsets=[], total_bytes=30)

    assert len(receipts) == 1
    assert receipts[0].ops == ()
    assert receipts[0].byte_count == 30


def test_total_bytes_yields_no_placeholder_receipt_when_nothing_has_arrived():
    receipts = split_into_receipts([], byte_offsets=[], total_bytes=0)

    assert receipts == []
