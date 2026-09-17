"""Tests for the render-operation value objects emitted by the parser.

Ops are plain dataclasses, so equality/field access is what matters —
the parser tests exercise them thoroughly in context.
"""

from core.ops import CutOp, FeedOp, TextOp
from core.state import PrinterState


def test_text_op_carries_text_and_style_snapshot():
    style = PrinterState()
    op = TextOp(text="hello", style=style)

    assert op.text == "hello"
    assert op.style is style


def test_text_op_raw_defaults_to_empty_bytes():
    # Additive field (see core.parser._parse_text): existing callers that
    # only ever cared about the decoded text must keep working unchanged.
    op = TextOp(text="hello", style=PrinterState())

    assert op.raw == b""


def test_feed_op_equality_by_value():
    assert FeedOp(lines=3) == FeedOp(lines=3)
    assert FeedOp(lines=3) != FeedOp(lines=4)


def test_cut_op_defaults_feed_lines_to_zero():
    op = CutOp(mode="full")

    assert op.feed_lines == 0
