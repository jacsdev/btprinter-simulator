"""Tests for core.parser.Parser.take_op_offsets(): the per-op byte-offset
accessor that lets a caller compute accurate per-receipt byte counts
(see render.receipts.split_into_receipts's `byte_offsets` parameter)
instead of approximating from the raw chunk size.

This is purely additive bookkeeping -- see
test_parsing_behaviour_is_unchanged_alongside_op_offsets below for the
regression guard proving feed()'s return value and existing diagnostics
behaviour are untouched.
"""

from core.ops import CutOp, LineFeedOp, TextOp
from core.parser import Parser


def test_take_op_offsets_returns_one_entry_per_op():
    parser = Parser()

    ops = parser.feed(b"HI\x0a")  # TextOp("HI") + LineFeedOp

    offsets = parser.take_op_offsets()
    assert len(offsets) == len(ops) == 2


def test_take_op_offsets_are_relative_to_the_chunk_and_reflect_bytes_actually_consumed():
    parser = Parser()

    # "AB" (2 bytes, TextOp) + LF (1 byte, LineFeedOp) + "CD" (2 bytes, TextOp)
    ops = parser.feed(b"AB\x0aCD")

    offsets = parser.take_op_offsets()
    assert len(ops) == 3
    assert offsets == [2, 3, 5]


def test_take_op_offsets_gives_distinct_offsets_for_ops_in_the_same_feed_call():
    # This is the exact shape of the session-history bug: a single feed()
    # call spanning two receipts must not attribute the same offset to
    # every op in it.
    parser = Parser()

    data = b"A" + bytes([0x0a]) + bytes([0x1D, 0x56, 0x00]) + b"B" + bytes([0x0a])
    ops = parser.feed(data)  # TextOp, LineFeedOp, CutOp, TextOp, LineFeedOp

    offsets = parser.take_op_offsets()
    assert len(offsets) == len(ops)
    # Strictly increasing: no two ops share the same cumulative offset.
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)
    assert offsets[-1] == len(data)


def test_take_op_offsets_last_value_equals_total_bytes_for_a_fully_consumed_stream():
    parser = Parser()
    data = b"HELLO WORLD\x0a" + bytes([0x1D, 0x56, 0x00])

    parser.feed(data)

    offsets = parser.take_op_offsets()
    assert offsets[-1] == len(data)


def test_take_op_offsets_drains_the_list():
    parser = Parser()
    parser.feed(b"A\x0a")

    first = parser.take_op_offsets()
    second = parser.take_op_offsets()

    assert len(first) == 2
    assert second == []


def test_take_op_offsets_accumulate_across_feed_calls_until_drained():
    parser = Parser()
    parser.feed(b"A\x0a")
    parser.feed(b"B\x0a")

    offsets = parser.take_op_offsets()
    assert len(offsets) == 4


def test_ops_with_zero_length_result_contribute_no_offset_entries():
    # CR is consumed with no op emitted; it must not desync the offsets
    # list from the ops list.
    parser = Parser()

    ops = parser.feed(b"A\x0d\x0a")  # 'A' (TextOp), CR (no op), LF (LineFeedOp)

    offsets = parser.take_op_offsets()
    assert len(ops) == 2
    assert len(offsets) == 2


def test_parsing_behaviour_is_unchanged_alongside_op_offsets():
    # Regression guard: adding the offsets channel must not alter what
    # valid, well-formed commands parse into.
    parser = Parser()

    ops = parser.feed(b"OK\x0a" + bytes([0x1D, 0x56, 0x00]))

    assert isinstance(ops[0], TextOp) and ops[0].text == "OK"
    assert isinstance(ops[1], LineFeedOp)
    assert isinstance(ops[2], CutOp)
