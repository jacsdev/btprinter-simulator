"""Render operations emitted by `core.parser.Parser`.

Each op is a plain, immutable-by-convention dataclass. The parser never
imports anything from `render/` or `transport/` — it only produces this
vocabulary of operations, which the render layer interprets. This is the
hard boundary described in the README: on real 58mm hardware the Bluetooth
module is just an SPP-to-UART bridge, and the print MCU consuming these
same ESC/POS bytes has no notion of Bluetooth, TCP, or any other
transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.state import PrinterState


@dataclass
class TextOp:
    """A run of printable text rendered with the style active at the time."""

    text: str
    style: PrinterState


@dataclass
class LineFeedOp:
    """LF: print the current line buffer and feed one line."""


@dataclass
class FeedOp:
    """ESC d n: feed n lines without printing."""

    lines: int


@dataclass
class ReverseFeedOp:
    """ESC e n: feed n lines backward (if the mechanism supports it)."""

    lines: int


@dataclass
class InitOp:
    """ESC @: initialize the printer, resetting style state to defaults."""


@dataclass
class StyleChangeOp:
    """A style-only command (align, bold, underline, size, ...).

    Carries a full snapshot of the state after the change, mainly for
    observability/testing. Renderers primarily rely on the style embedded
    in each `TextOp`.
    """

    style: PrinterState


@dataclass
class LineSpacingOp:
    """ESC 2 (default) / ESC 3 n (set line spacing in dots)."""

    dots: Optional[int]  # None means "restore default spacing"


@dataclass
class CutOp:
    """GS V: paper cut, optionally preceded by a feed."""

    mode: str  # "full" or "partial"
    feed_lines: int = 0


@dataclass
class CashDrawerOp:
    """ESC p m t1 t2: pulse the cash drawer connector. Nothing to draw."""

    pin: int
    on_time: int
    off_time: int


@dataclass
class BitImageOp:
    """ESC *: column-format bit image."""

    mode: int
    width: int  # dot columns
    height: int  # dots per column (8 or 24 depending on mode)
    data: bytes
    # Horizontal print position (left_margin_dots + h_pos_dots) active when
    # this op was parsed -- see ESC $ / ESC \ / GS L in core/state.py.
    x_offset: int = 0


@dataclass
class RasterImageOp:
    """GS v 0: raster-format bit image, decoded to pixel dimensions."""

    width: int  # pixels
    height: int  # pixels
    data: bytes  # row-major, 1 bit per pixel, MSB first, padded to a byte
    # Horizontal print position (left_margin_dots + h_pos_dots) active when
    # this op was parsed -- see ESC $ / ESC \ / GS L in core/state.py.
    x_offset: int = 0


@dataclass
class BarcodeOp:
    """GS k: print a 1D barcode of the given symbology."""

    symbology: str
    data: bytes


@dataclass
class QrStoreOp:
    """GS ( k ... fn=80: store QR symbol data in the print buffer."""

    data: bytes


@dataclass
class QrPrintOp:
    """GS ( k ... fn=81: print the previously stored QR symbol."""

    model: int
    size: int
    error_correction: int
