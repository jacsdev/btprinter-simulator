"""Render ops -> a PIL Image that looks like the physical receipt paper.

This module only depends on `core.ops` and `core.state` (read-only, for
the op/style vocabulary) plus PIL. It never imports from `transport/`,
keeping the transport -> core -> render data flow one-directional, the
same way a print head never talks back to the Bluetooth module.

Paper metrics (203 dpi, verified):
    58mm -> 384 dots, 80mm -> 576 dots.
    Font A: 12x24 dots/char -> 32 chars/line at 384, 48 at 576.
    Font B: 9x17 dots/char -> 42 chars/line at 384, 64 at 576.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from core.ops import (
    BarcodeOp,
    BitImageOp,
    CutOp,
    FeedOp,
    InitOp,
    LineFeedOp,
    QrPrintOp,
    QrStoreOp,
    RasterImageOp,
    ReverseFeedOp,
    StyleChangeOp,
    TextOp,
)
from core.state import PrinterState

logger = logging.getLogger("btprinter.render")

# (dot width, dot height) per character cell, at 203 dpi.
FONT_METRICS: Dict[str, Tuple[int, int]] = {
    "A": (12, 24),
    "B": (9, 17),
}

_DEFAULT_MONOSPACE_CANDIDATES = [
    "C:\\Windows\\Fonts\\consola.ttf",
    "C:\\Windows\\Fonts\\cour.ttf",
    "C:\\Windows\\Fonts\\lucon.ttf",
]

_BACKGROUND = 255  # white
_FOREGROUND = 0  # black
_LINE_MARGIN = 4  # vertical padding added per printed line, in dots
_DEFAULT_LINE_HEIGHT = 24

_font_cache: Dict[Tuple[str, int, int], ImageFont.FreeTypeFont] = {}


def chars_per_line(width_dots: int, font: str) -> int:
    """Return how many monospace characters fit on one line for `font`."""
    cell_width, _ = FONT_METRICS[font]
    return width_dots // cell_width


def wrap_text_hard(text: str, max_chars: int) -> List[str]:
    """Hard-wrap `text` into chunks of at most `max_chars` characters.

    Matches measured real-printer behavior: a thermal print head counts
    character cells, not words, so overflowing text wraps at the exact
    column boundary -- mid-word if that is where the limit falls (e.g.
    "Pende" / "ntes: 2") -- rather than being truncated or word-wrapped.

    Always returns at least one chunk (`[""]` for empty input), and the
    chunks always concatenate back to exactly `text` with nothing lost
    or trimmed. A line exactly at `max_chars` is returned as a single
    chunk (no trailing empty chunk).
    """
    limit = max(max_chars, 1)
    if not text:
        return [""]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def ruler_x_position(width_dots: int, font: str) -> int:
    """Return the pixel x-position of the character-column limit guide
    (the "32-column ruler") for `font` at `width_dots`.

    This is `chars_per_line(width_dots, font)` columns converted back to
    dots, e.g. 32 columns * 12 dots/column = 384 for 58mm/Font A. Pure
    arithmetic, independent of Tk -- the viewer only needs to draw a
    vertical line at this x-coordinate.
    """
    cell_width, _ = FONT_METRICS[font]
    return chars_per_line(width_dots, font) * cell_width


def load_font(path: str, target_width: int, target_height: int) -> ImageFont.ImageFont:
    """Load a monospace TTF sized so its advance width matches the dot
    grid, falling back to PIL's built-in bitmap font if the file is
    missing or unreadable. Never raises."""
    cache_key = (path, target_width, target_height)
    if cache_key in _font_cache:
        return _font_cache[cache_key]

    best_font: Optional[ImageFont.FreeTypeFont] = None
    best_diff: Optional[int] = None
    try:
        for size in range(6, 49):
            candidate = ImageFont.truetype(path, size)
            width = candidate.getlength("0")
            diff = abs(width - target_width)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_font = candidate
    except OSError:
        logger.warning("font file not found or unreadable: %s, using default", path)
        best_font = None

    font = best_font or ImageFont.load_default()
    _font_cache[cache_key] = font
    return font


def _resolve_font(style: PrinterState) -> ImageFont.ImageFont:
    cell_width, cell_height = FONT_METRICS.get(style.font, FONT_METRICS["A"])
    for path in _DEFAULT_MONOSPACE_CANDIDATES:
        try:
            return load_font(path, cell_width, cell_height)
        except Exception:  # pragma: no cover - defensive, load_font already guards
            continue
    return ImageFont.load_default()


class ReceiptRenderer:
    """Converts a list of render ops into a single receipt-shaped image.

    The image grows downward as lines/feeds/images are appended; callers
    that want a live, incrementally updated receipt should accumulate ops
    and re-render (see `render/viewer.py`).
    """

    def __init__(self, width_dots: int = 384) -> None:
        self.width_dots = width_dots

    def render(self, ops: List[object]) -> Image.Image:
        """Render the full op list to a PIL Image, top to bottom."""
        segments: List[Image.Image] = []
        pending_text: List[TextOp] = []

        def flush_line() -> None:
            if pending_text:
                segments.append(self._render_text_line(pending_text))
                pending_text.clear()

        for op in ops:
            try:
                if isinstance(op, TextOp):
                    pending_text.append(op)
                elif isinstance(op, LineFeedOp):
                    flush_line()
                elif isinstance(op, FeedOp):
                    flush_line()
                    segments.append(self._blank(op.lines * _DEFAULT_LINE_HEIGHT))
                elif isinstance(op, ReverseFeedOp):
                    flush_line()
                    # Cannot un-print already-rendered pixels; represented
                    # as a no-op spacer for visual continuity.
                    segments.append(self._blank(0))
                elif isinstance(op, CutOp):
                    flush_line()
                    segments.append(self._render_cut_marker(op))
                elif isinstance(op, RasterImageOp):
                    flush_line()
                    segments.append(self._render_raster(op))
                elif isinstance(op, BitImageOp):
                    flush_line()
                    segments.append(self._render_bit_image(op))
                elif isinstance(op, BarcodeOp):
                    flush_line()
                    segments.append(self._render_placeholder_block(
                        f"[{op.symbology}]", op.data.decode("ascii", errors="replace")
                    ))
                elif isinstance(op, QrStoreOp):
                    self._pending_qr_data = op.data
                elif isinstance(op, QrPrintOp):
                    flush_line()
                    data = getattr(self, "_pending_qr_data", b"")
                    label = f"[QR model={op.model} size={op.size} ec={op.error_correction}]"
                    segments.append(
                        self._render_placeholder_block(
                            label, data.decode("utf-8", errors="replace")
                        )
                    )
                elif isinstance(op, (InitOp, StyleChangeOp)):
                    pass  # style-only, nothing to draw
                # Unknown/unsupported op types are silently ignored: the
                # renderer must never crash the live viewer over an op it
                # does not yet know how to draw.
            except Exception:
                logger.exception("failed to render op %r, skipping", op)

        flush_line()

        if not segments:
            return Image.new("L", (self.width_dots, 1), color=_BACKGROUND)

        total_height = sum(img.height for img in segments) or 1
        canvas = Image.new("L", (self.width_dots, total_height), color=_BACKGROUND)
        y = 0
        for segment in segments:
            canvas.paste(segment, (0, y))
            y += segment.height
        return canvas

    # -- text -----------------------------------------------------------

    def _render_text_line(self, runs: List[TextOp]) -> Image.Image:
        style = runs[-1].style if runs else PrinterState()
        width_mult = max(style.width_mult, 1)
        height_mult = max(style.height_mult, 1)

        text = "".join(run.text for run in runs)
        font = _resolve_font(style)

        # Hard-wrap at the column limit for this style: a real print head
        # continues overflowing text on the next physical line instead of
        # losing it (measured on paper -- see tests/test_raster_wrap.py).
        # Double-width text consumes 2 cells/char, so the limit shrinks by
        # `width_mult`; reuse `chars_per_line` rather than duplicating the
        # per-font dot metrics.
        max_chars = chars_per_line(self.width_dots, style.font) // width_mult
        chunks = wrap_text_hard(text, max_chars)

        # Each wrapped chunk becomes its own physical line, at the same
        # height as (and inheriting the alignment of) the line it
        # continues from.
        chunk_lines = [
            self._render_text_chunk(chunk, style, font, width_mult, height_mult)
            for chunk in chunks
        ]

        total_height = sum(chunk_line.height for chunk_line in chunk_lines) or 1
        canvas = Image.new("L", (self.width_dots, total_height), color=_BACKGROUND)
        y = 0
        for chunk_line in chunk_lines:
            canvas.paste(chunk_line, (0, y))
            y += chunk_line.height
        return canvas

    def _render_text_chunk(
        self,
        text: str,
        style: PrinterState,
        font: ImageFont.ImageFont,
        width_mult: int,
        height_mult: int,
    ) -> Image.Image:
        """Render one already-wrapped chunk as a single physical line."""
        cell_width, cell_height = FONT_METRICS.get(style.font, FONT_METRICS["A"])

        # Draw at base (1x) size first, then upscale with nearest-neighbor.
        # Real thermal heads implement GS ! size multipliers by physically
        # repeating dot rows/columns, not by smooth interpolation, so
        # NEAREST reproduces the same blocky look instead of anti-aliasing
        # it away.
        base_height = cell_height + _LINE_MARGIN
        try:
            base_width = max(int(font.getlength(text)), 1)
        except Exception:
            base_width = max(len(text) * cell_width, 1)

        base = Image.new("L", (base_width, base_height), color=_BACKGROUND)
        draw = ImageDraw.Draw(base)
        fill = _FOREGROUND
        if style.reverse:
            draw.rectangle([0, 0, base_width, base_height], fill=_FOREGROUND)
            fill = _BACKGROUND
        draw.text((0, 0), text, font=font, fill=fill)
        if style.underline:
            underline_y = base_height - 2
            draw.line([(0, underline_y), (base_width, underline_y)], fill=fill)

        scaled = base.resize(
            (base_width * width_mult, base_height * height_mult), Image.NEAREST
        )

        line_height = max(scaled.height, 1)
        line = Image.new(
            "L", (self.width_dots, line_height), color=_BACKGROUND if not style.reverse else _FOREGROUND
        )

        if style.align == "center":
            x = max((self.width_dots - scaled.width) // 2, 0)
        elif style.align == "right":
            x = max(self.width_dots - scaled.width, 0)
        else:
            x = 0

        line.paste(scaled, (x, 0))
        return line

    # -- images -----------------------------------------------------------

    def _render_raster(self, op: RasterImageOp) -> Image.Image:
        width, height = max(op.width, 1), max(op.height, 1)
        row_bytes = (width + 7) // 8
        image = Image.new("1", (width, height), color=1)  # 1 = white
        pixels = image.load()
        for row in range(height):
            offset = row * row_bytes
            row_data = op.data[offset : offset + row_bytes]
            for col in range(width):
                byte_index = col // 8
                if byte_index >= len(row_data):
                    continue
                bit = 7 - (col % 8)
                if row_data[byte_index] & (1 << bit):
                    pixels[col, row] = 0  # black
        canvas = Image.new("L", (self.width_dots, height), color=_BACKGROUND)
        canvas.paste(image.convert("L"), (0, 0))
        return canvas

    def _render_bit_image(self, op: BitImageOp) -> Image.Image:
        # ESC * is column-major: each byte is a vertical strip of bits.
        width, height = max(op.width, 1), max(op.height, 1)
        bytes_per_column = height // 8
        image = Image.new("1", (width, height), color=1)
        pixels = image.load()
        for col in range(width):
            for byte_row in range(bytes_per_column):
                index = col * bytes_per_column + byte_row
                if index >= len(op.data):
                    continue
                byte = op.data[index]
                for bit in range(8):
                    if byte & (1 << (7 - bit)):
                        y = byte_row * 8 + bit
                        if y < height:
                            pixels[col, y] = 0
        canvas = Image.new("L", (self.width_dots, height), color=_BACKGROUND)
        canvas.paste(image.convert("L"), (0, 0))
        return canvas

    # -- placeholders / markers ------------------------------------------

    def _render_placeholder_block(self, label: str, data_text: str) -> Image.Image:
        # Getting the parsing right matters more than a scannable symbol,
        # so barcodes/QR codes are drawn as a clearly labeled box (per
        # the slice-1 spec) rather than a real, scannable glyph.
        height = 60
        image = Image.new("L", (self.width_dots, height), color=_BACKGROUND)
        draw = ImageDraw.Draw(image)
        margin = 4
        draw.rectangle(
            [margin, margin, self.width_dots - margin, height - margin],
            outline=_FOREGROUND,
        )
        font = ImageFont.load_default()
        draw.text((margin + 4, margin + 4), label, font=font, fill=_FOREGROUND)
        draw.text((margin + 4, margin + 20), data_text[:60], font=font, fill=_FOREGROUND)
        return image

    def _render_cut_marker(self, op: CutOp) -> Image.Image:
        height = 8 + op.feed_lines * _DEFAULT_LINE_HEIGHT
        image = Image.new("L", (self.width_dots, max(height, 1)), color=_BACKGROUND)
        draw = ImageDraw.Draw(image)
        dash_y = height - 4
        dash_len, gap = 6, 4
        x = 0
        while x < self.width_dots:
            draw.line([(x, dash_y), (min(x + dash_len, self.width_dots), dash_y)], fill=_FOREGROUND)
            x += dash_len + gap
        return image

    def _blank(self, height: int) -> Image.Image:
        return Image.new("L", (self.width_dots, max(height, 1)), color=_BACKGROUND)
