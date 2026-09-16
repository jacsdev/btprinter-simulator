"""Active print state tracked while parsing an ESC/POS byte stream.

The state mirrors the style registers a real thermal print head keeps
between commands: alignment, emphasis, underline, font, character size
multipliers, reverse video, orientation and the currently configured
barcode geometry. `ESC @` resets it to power-on defaults.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BarcodeConfig:
    """Barcode geometry/HRI configuration set by GS h / GS w / GS f / GS H."""

    height: int = 162
    width: int = 3
    font: int = 0
    hri_position: int = 0  # 0=none, 1=above, 2=below, 3=both


@dataclass
class PrinterState:
    """Style attributes that apply to subsequently printed text/graphics."""

    align: str = "left"  # "left", "center", "right"
    bold: bool = False
    underline: int = 0  # 0=off, 1=single, 2=double
    font: str = "A"  # "A" (12x24) or "B" (9x17)
    width_mult: int = 1  # 1-8
    height_mult: int = 1  # 1-8
    reverse: bool = False  # white on black
    upside_down: bool = False
    rotate90: bool = False
    line_spacing: Optional[int] = None  # dots; None = printer default
    code_table: int = 0  # active codepage id selected by ESC t; see core.commands.CODEPAGE_MAP
    charset: int = 0
    kanji_mode: bool = False  # set by FS & / FS .; Kanji rendering itself is out of scope
    barcode: BarcodeConfig = field(default_factory=BarcodeConfig)
    # Print-position family (ESC $, ESC \, GS L, GS W). All four are dot
    # measurements and all four persist across lines -- like every other
    # field on this class -- until explicitly changed again or reset by
    # ESC @, matching the documented Epson ESC/POS behavior for these
    # commands.
    h_pos_dots: int = 0  # ESC $ (absolute) / ESC \ (relative): dots from left_margin_dots
    left_margin_dots: int = 0  # GS L: left margin, dots from the physical paper edge
    print_area_width_dots: Optional[int] = None  # GS W: printing area width, dots; None = full paper width minus the left margin

    def reset(self) -> None:
        """Restore power-on defaults, as triggered by ESC @ (initialize)."""
        default = PrinterState()
        self.align = default.align
        self.bold = default.bold
        self.underline = default.underline
        self.font = default.font
        self.width_mult = default.width_mult
        self.height_mult = default.height_mult
        self.reverse = default.reverse
        self.upside_down = default.upside_down
        self.rotate90 = default.rotate90
        self.line_spacing = default.line_spacing
        self.code_table = default.code_table
        self.charset = default.charset
        self.kanji_mode = default.kanji_mode
        self.barcode = default.barcode
        self.h_pos_dots = default.h_pos_dots
        self.left_margin_dots = default.left_margin_dots
        self.print_area_width_dots = default.print_area_width_dots

    def snapshot(self) -> "PrinterState":
        """Return a deep, independent copy to tag onto a render op."""
        return copy.deepcopy(self)
