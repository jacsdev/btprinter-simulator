"""Tests for render.raster.ReceiptRenderer honoring the print-position
family (ESC $, ESC \\, GS L, GS W) -- see core/state.py and core/parser.py.

These assert real pixel positions in the rendered image, not just parser
state: a parser that consumes a positioning command and then ignores it
would silently mis-place content, the same class of defect this project
has already fixed for status reporting elsewhere.
"""

from PIL import Image

from core.ops import BitImageOp, LineFeedOp, RasterImageOp, TextOp
from core.state import PrinterState
from render.raster import ReceiptRenderer, chars_per_line


def _black_bbox(image: Image.Image):
    mask = image.convert("L").point(lambda p: 255 if p < 128 else 0)
    return mask.getbbox()


# -- ESC $ / ESC \: horizontal text position -------------------------------


def test_zero_h_pos_places_left_aligned_text_flush_left():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState(h_pos_dots=0)
    ops = [TextOp(text="A", style=style), LineFeedOp()]

    image = renderer.render(ops)

    bbox = _black_bbox(image)
    assert bbox is not None
    assert bbox[0] == 0


def test_nonzero_h_pos_shifts_left_aligned_text_right_by_that_many_dots():
    renderer = ReceiptRenderer(width_dots=384)
    zero_pos_style = PrinterState(h_pos_dots=0)
    offset_style = PrinterState(h_pos_dots=100)

    zero_image = renderer.render([TextOp(text="A", style=zero_pos_style), LineFeedOp()])
    offset_image = renderer.render([TextOp(text="A", style=offset_style), LineFeedOp()])

    zero_bbox = _black_bbox(zero_image)
    offset_bbox = _black_bbox(offset_image)
    assert zero_bbox is not None
    assert offset_bbox is not None
    assert offset_bbox[0] - zero_bbox[0] == 100


def test_h_pos_only_applies_to_the_first_wrapped_physical_line():
    # ESC $/ESC \\ set the print *start* position (Epson reference: "in
    # effect only ... at the beginning of a line"). A continuation line
    # produced by hard-wrap starts back at the left margin, like any other
    # fresh line, rather than repeating the same absolute offset.
    renderer = ReceiptRenderer(width_dots=384)
    # A reduced printable width (200 dots -> 16 columns) leaves enough
    # canvas room for the 50-dot offset without hitting the right edge.
    style = PrinterState(h_pos_dots=50, print_area_width_dots=200)
    text = "A" * 20  # > 16 columns -> wraps once

    image = renderer.render([TextOp(text=text, style=style), LineFeedOp()])

    line_height = image.height // 2
    first_line = image.crop((0, 0, image.width, line_height))
    second_line = image.crop((0, line_height, image.width, image.height))

    first_bbox = _black_bbox(first_line)
    second_bbox = _black_bbox(second_line)
    assert first_bbox is not None
    assert second_bbox is not None
    assert first_bbox[0] == 50
    assert second_bbox[0] == 0


# -- GS L / GS W: left margin and printing area width ----------------------


def test_gs_l_left_margin_shifts_left_aligned_text():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState(left_margin_dots=40)
    ops = [TextOp(text="A", style=style), LineFeedOp()]

    image = renderer.render(ops)

    bbox = _black_bbox(image)
    assert bbox is not None
    assert bbox[0] == 40


def test_gs_w_reduces_usable_width_and_therefore_wrap_column_count():
    renderer = ReceiptRenderer(width_dots=384)
    narrow_width = 192  # half of 384 dots -> 16 columns instead of 32 for Font A
    style = PrinterState(print_area_width_dots=narrow_width)
    expected_columns = chars_per_line(narrow_width, "A")
    assert expected_columns == 16

    at_limit = renderer.render(
        [TextOp(text="A" * expected_columns, style=style), LineFeedOp()]
    )
    one_over = renderer.render(
        [TextOp(text="A" * (expected_columns + 1), style=style), LineFeedOp()]
    )
    single_line_height = renderer.render(
        [TextOp(text="A", style=style), LineFeedOp()]
    ).height

    assert at_limit.height == single_line_height
    assert one_over.height == single_line_height * 2


def test_gs_l_left_margin_reduces_usable_width_when_gs_w_not_set():
    # With no explicit GS W, the printable area runs from the left margin
    # to the paper's right edge, so a nonzero margin also shrinks the wrap
    # column count -- inferred from the Epson command reference, not from
    # a captured multi-column sample (see the note in render/raster.py).
    renderer = ReceiptRenderer(width_dots=384)
    margin = 384 - 192  # leaves 192 usable dots, same 16-column budget
    style = PrinterState(left_margin_dots=margin)
    expected_columns = chars_per_line(192, "A")

    at_limit = renderer.render(
        [TextOp(text="A" * expected_columns, style=style), LineFeedOp()]
    )
    one_over = renderer.render(
        [TextOp(text="A" * (expected_columns + 1), style=style), LineFeedOp()]
    )
    single_line_height = renderer.render(
        [TextOp(text="A", style=style), LineFeedOp()]
    ).height

    assert at_limit.height == single_line_height
    assert one_over.height == single_line_height * 2


def test_center_alignment_is_computed_within_the_gs_w_printable_area():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState(align="center", print_area_width_dots=192, left_margin_dots=0)
    ops = [TextOp(text="A", style=style), LineFeedOp()]

    image = renderer.render(ops)

    bbox = _black_bbox(image)
    assert bbox is not None
    bbox_center = (bbox[0] + bbox[2]) / 2
    # Centered within the first 192 dots, not the full 384-dot paper.
    assert abs(bbox_center - 96) < 12


# -- images: RasterImageOp / BitImageOp x_offset ----------------------------


def test_raster_image_renders_at_its_x_offset():
    renderer = ReceiptRenderer(width_dots=384)
    width_px, height_px = 16, 4
    row_bytes = width_px // 8
    data = bytes([0xFF] * (row_bytes * height_px))
    op = RasterImageOp(width=width_px, height=height_px, data=data, x_offset=50)

    image = renderer.render([op])

    bbox = _black_bbox(image)
    assert bbox is not None
    assert bbox[0] == 50


def test_raster_image_zero_x_offset_stays_flush_left_regression():
    # This is the exact shape of the real capture: ESC $ 00 00 (x_offset=0)
    # around an image must not shift it.
    renderer = ReceiptRenderer(width_dots=384)
    width_px, height_px = 16, 4
    row_bytes = width_px // 8
    data = bytes([0xFF] * (row_bytes * height_px))
    op = RasterImageOp(width=width_px, height=height_px, data=data, x_offset=0)

    image = renderer.render([op])

    bbox = _black_bbox(image)
    assert bbox is not None
    assert bbox[0] == 0


def test_bit_image_renders_at_its_x_offset():
    renderer = ReceiptRenderer(width_dots=384)
    columns = 2
    data = bytes([0xFF, 0xFF])  # 1 byte per column, single density
    op = BitImageOp(mode=0, width=columns, height=8, data=data, x_offset=30)

    image = renderer.render([op])

    bbox = _black_bbox(image)
    assert bbox is not None
    assert bbox[0] == 30


def test_raster_image_x_offset_is_clamped_to_stay_on_the_canvas():
    renderer = ReceiptRenderer(width_dots=384)
    width_px, height_px = 16, 4
    row_bytes = width_px // 8
    data = bytes([0xFF] * (row_bytes * height_px))
    op = RasterImageOp(width=width_px, height=height_px, data=data, x_offset=1000)

    image = renderer.render([op])  # must not raise

    bbox = _black_bbox(image)
    assert bbox is not None
    assert bbox[2] <= 384
