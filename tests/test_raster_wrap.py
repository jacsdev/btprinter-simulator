"""Tests for hard line-wrap fidelity in render.raster.

Physical printer evidence (verified against a real 58mm thermal printer,
NOT inferred): overflowing text WRAPS onto the next physical line with a
hard, non-word-aware break at the column boundary, and the continuation
line inherits the original line's alignment. The simulator used to
silently truncate at the column boundary and lose the overflow -- these
tests pin the corrected, paper-matching behavior using the three
measured fixtures from the same ticket:

    "LISTA DE EMBARQUE" at double width on 58mm/Font A ->
        "LISTA DE EMBARQU" then a centered "E"
    "Total: 5   Embarcados: 3   Pendentes: 2" ->
        breaks at exactly column 32, continuation left-aligned
    "Prototipo de viabilidade - Soft-TI" ->
        "Prototipo de viabilidade - Soft-" then centered "TI"
"""

import pytest
from PIL import Image

from core.ops import LineFeedOp, TextOp
from core.state import PrinterState
from render.raster import ReceiptRenderer, chars_per_line, wrap_text_hard


def _black_bbox(image: Image.Image):
    mask = image.convert("L").point(lambda p: 255 if p < 128 else 0)
    return mask.getbbox()


# -- wrap_text_hard: pure function, no PIL, no rendering --------------------


def test_wrap_hard_splits_mid_word_at_exact_column_boundary():
    # Measured: "...Pende|ntes: 2" -- hard break, no word-awareness.
    chunks = wrap_text_hard("Total: 5   Embarcados: 3   Pendentes: 2", 32)
    assert chunks == ["Total: 5   Embarcados: 3   Pende", "ntes: 2"]


def test_wrap_hard_splits_mid_word_soft_ti_fixture():
    chunks = wrap_text_hard("Prototipo de viabilidade - Soft-TI", 32)
    assert chunks == ["Prototipo de viabilidade - Soft-", "TI"]


def test_wrap_hard_line_at_exact_limit_does_not_wrap():
    text = "A" * 32
    assert wrap_text_hard(text, 32) == [text]


def test_wrap_hard_line_one_over_limit_wraps_into_two():
    text = "A" * 33
    assert wrap_text_hard(text, 32) == ["A" * 32, "A"]


def test_wrap_hard_loops_for_more_than_two_lines():
    text = "A" * 70
    assert wrap_text_hard(text, 32) == ["A" * 32, "A" * 32, "A" * 6]


def test_wrap_hard_empty_text_yields_single_empty_chunk():
    assert wrap_text_hard("", 32) == [""]


@pytest.mark.parametrize(
    "text,limit",
    [
        ("LISTA DE EMBARQUE", 16),
        ("Total: 5   Embarcados: 3   Pendentes: 2", 32),
        ("Prototipo de viabilidade - Soft-TI", 32),
        ("-" * 50, 32),
        ("x", 32),
        ("y" * 32, 32),
        ("z" * 200, 42),
        ("", 10),
    ],
)
def test_wrap_hard_never_loses_a_character(text, limit):
    chunks = wrap_text_hard(text, limit)
    assert "".join(chunks) == text


# -- integration through ReceiptRenderer -------------------------------------


def test_double_width_text_wraps_after_16_chars_on_58mm():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState(width_mult=2, align="center")
    ops = [TextOp(text="LISTA DE EMBARQUE", style=style), LineFeedOp()]

    image = renderer.render(ops)

    # one wrapped TextOp line -> two stacked physical lines of equal height
    assert image.height % 2 == 0
    line_height = image.height // 2
    first_line = image.crop((0, 0, image.width, line_height))
    second_line = image.crop((0, line_height, image.width, image.height))

    assert _black_bbox(first_line) is not None  # "LISTA DE EMBARQU" drawn
    second_bbox = _black_bbox(second_line)
    assert second_bbox is not None  # "E" not lost

    # continuation ("E") must be centered, not flush-left/right
    bbox_center = (second_bbox[0] + second_bbox[2]) / 2
    assert abs(bbox_center - image.width / 2) < image.width * 0.15


def test_total_line_wraps_at_32_chars_left_aligned():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState(align="left")
    text = "Total: 5   Embarcados: 3   Pendentes: 2"
    ops = [TextOp(text=text, style=style), LineFeedOp()]

    image = renderer.render(ops)

    assert image.height % 2 == 0
    line_height = image.height // 2
    second_line = image.crop((0, line_height, image.width, image.height))
    bbox = _black_bbox(second_line)
    assert bbox is not None  # "ntes: 2" not lost
    assert bbox[0] < image.width * 0.1  # flush left, not centered/right


def test_right_aligned_continuation_stays_flush_right():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState(align="right")
    text = "X" * 40  # 40 > 32 columns -> wraps once
    ops = [TextOp(text=text, style=style), LineFeedOp()]

    image = renderer.render(ops)
    line_height = image.height // 2
    second_line = image.crop((0, line_height, image.width, image.height))
    bbox = _black_bbox(second_line)
    assert bbox is not None
    assert (image.width - bbox[2]) < image.width * 0.1


def test_line_requiring_three_lines_wraps_twice():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState()
    text = "A" * 70  # > 2 * 32 columns -> must loop, not split once
    ops = [TextOp(text=text, style=style), LineFeedOp()]

    image = renderer.render(ops)
    single_line_height = renderer.render(
        [TextOp(text="A", style=style), LineFeedOp()]
    ).height

    assert image.height == single_line_height * 3


def test_line_exactly_at_limit_does_not_wrap_off_by_one_guard():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState()
    at_limit = renderer.render([TextOp(text="A" * 32, style=style), LineFeedOp()])
    one_over = renderer.render([TextOp(text="A" * 33, style=style), LineFeedOp()])
    single_line_height = renderer.render(
        [TextOp(text="A", style=style), LineFeedOp()]
    ).height

    assert at_limit.height == single_line_height
    assert one_over.height == single_line_height * 2


def test_hr_separator_line_wraps_like_any_other_text():
    # A separator such as `hr()` in the source app is just a long run of
    # a repeated character; it goes through the same TextOp render path
    # so it must wrap identically. Measured on paper: a separator longer
    # than the column limit wrapped onto a second line too.
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState()
    ops = [TextOp(text="-" * 40, style=style), LineFeedOp()]

    image = renderer.render(ops)
    single_line_height = renderer.render(
        [TextOp(text="-", style=style), LineFeedOp()]
    ).height

    assert image.height == single_line_height * 2


def test_row_columns_concatenated_text_wraps_too():
    # row()-style column layouts are built by concatenating TextOp runs
    # into one logical line before LF; wrapping must apply to the joined
    # text just like any other overflowing line.
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState()
    ops = [
        TextOp(text="A" * 20, style=style),
        TextOp(text="B" * 20, style=style),
        LineFeedOp(),
    ]

    image = renderer.render(ops)
    single_line_height = renderer.render(
        [TextOp(text="A", style=style), LineFeedOp()]
    ).height

    assert image.height == single_line_height * 2


def test_wrapping_respects_font_b_metrics_on_80mm():
    renderer = ReceiptRenderer(width_dots=576)
    style = PrinterState(font="B")
    limit = chars_per_line(576, "B")
    assert limit == 64

    at_limit = renderer.render([TextOp(text="A" * limit, style=style), LineFeedOp()])
    one_over = renderer.render(
        [TextOp(text="A" * (limit + 1), style=style), LineFeedOp()]
    )
    single_line_height = renderer.render(
        [TextOp(text="A", style=style), LineFeedOp()]
    ).height

    assert at_limit.height == single_line_height
    assert one_over.height == single_line_height * 2
