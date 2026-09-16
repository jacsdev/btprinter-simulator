"""Tests for render.raster.ReceiptRenderer: render ops -> PIL Image."""

from PIL import Image

from core.ops import CutOp, FeedOp, LineFeedOp, RasterImageOp, TextOp
from core.state import PrinterState
from render.raster import ReceiptRenderer, load_font


def test_render_empty_ticket_has_configured_width():
    renderer = ReceiptRenderer(width_dots=384)

    image = renderer.render([])

    assert isinstance(image, Image.Image)
    assert image.width == 384


def test_render_respects_configurable_width_for_80mm():
    renderer = ReceiptRenderer(width_dots=576)

    image = renderer.render([])

    assert image.width == 576


def test_render_grows_taller_as_lines_are_added():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState()
    one_line_ops = [TextOp(text="A", style=style), LineFeedOp()]
    three_line_ops = one_line_ops * 3

    short_image = renderer.render(one_line_ops)
    tall_image = renderer.render(three_line_ops)

    assert tall_image.height > short_image.height


def test_render_places_raster_image_at_its_pixel_dimensions():
    renderer = ReceiptRenderer(width_dots=384)
    width_px, height_px = 16, 4
    # all-black raster data (every bit set)
    row_bytes = width_px // 8
    data = bytes([0xFF] * (row_bytes * height_px))
    ops = [RasterImageOp(width=width_px, height=height_px, data=data)]

    image = renderer.render(ops)

    assert image.height >= height_px


def test_render_never_raises_on_a_full_ticket_with_feed_and_cut():
    renderer = ReceiptRenderer(width_dots=384)
    style = PrinterState()
    ops = [
        TextOp(text="RECEIPT", style=style),
        LineFeedOp(),
        FeedOp(lines=3),
        CutOp(mode="full", feed_lines=0),
    ]

    image = renderer.render(ops)

    assert isinstance(image, Image.Image)


def test_gs_size_multiplier_visually_scales_rendered_text():
    # Discovered via the main.py + send_sample.py smoke test: GS ! must
    # actually scale the drawn glyphs, not just reserve taller line
    # spacing, otherwise "double size" text looks identical to normal text.
    renderer = ReceiptRenderer(width_dots=384)
    normal_style = PrinterState()
    big_style = PrinterState(width_mult=2, height_mult=2)

    normal_image = renderer.render([TextOp(text="A", style=normal_style), LineFeedOp()])
    big_image = renderer.render([TextOp(text="A", style=big_style), LineFeedOp()])

    def black_bbox(image: Image.Image):
        mask = image.convert("L").point(lambda p: 255 if p < 128 else 0)
        return mask.getbbox()

    normal_bbox = black_bbox(normal_image)
    big_bbox = black_bbox(big_image)
    assert normal_bbox is not None
    assert big_bbox is not None

    normal_width = normal_bbox[2] - normal_bbox[0]
    normal_height = normal_bbox[3] - normal_bbox[1]
    big_width = big_bbox[2] - big_bbox[0]
    big_height = big_bbox[3] - big_bbox[1]

    assert big_width > normal_width * 1.5
    assert big_height > normal_height * 1.5


def test_load_font_falls_back_to_default_when_file_missing():
    font = load_font("C:\\nonexistent\\path\\definitely-missing.ttf", 12, 24)

    assert font is not None
