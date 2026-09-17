"""Incremental ESC/POS byte stream parser.

Turns a raw ESC/POS byte stream (as it arrives over any transport) into a
list of render operations (see `core/ops.py`). This module has no
knowledge of Bluetooth, TCP, or Tkinter: it never imports from
`transport/` or `render/`. That boundary mirrors real 58mm printer
hardware, where the Bluetooth SPP module is a dumb byte pipe and the
print MCU that interprets ESC/POS commands has no notion of the
transport carrying them.

Robustness is the primary design constraint: bytes can arrive split at
any point (a single command spread across two socket reads), and
malformed or unsupported commands must never raise. Unknown input is
logged with its stream offset and skipped one byte at a time so the
parser can always resynchronize on the next recognizable command.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Set, Tuple

from core import charsets, commands
from core.diagnostics import Diagnostic
from core.ops import (
    BarcodeOp,
    BitImageOp,
    CashDrawerOp,
    CutOp,
    FeedOp,
    InitOp,
    LineFeedOp,
    LineSpacingOp,
    QrPrintOp,
    QrStoreOp,
    RasterImageOp,
    ReverseFeedOp,
    StyleChangeOp,
    TextOp,
)
from core.state import PrinterState

logger = logging.getLogger("btprinter.parser")

# GS k legacy (NUL-terminated) barcode symbologies, m = 0..6.
_BARCODE_LEGACY = {
    0: "UPC-A",
    1: "UPC-E",
    2: "EAN13",
    3: "EAN8",
    4: "CODE39",
    5: "ITF",
    6: "CODABAR",
}

# GS k length-prefixed barcode symbologies, m = 65..73.
_BARCODE_PREFIXED = {
    65: "UPC-A",
    66: "UPC-E",
    67: "EAN13",
    68: "EAN8",
    69: "CODE39",
    70: "ITF",
    71: "CODABAR",
    72: "CODE93",
    73: "CODE128",
}

_NEED_MORE = None  # sentinel returned by _try_parse_one when data is incomplete

# C0 control bytes with a recognized-but-inert meaning in this profile: real
# ESC/POS codes that this simulator deliberately does not model any effect
# for (page mode, tab stops, legacy dot-matrix double-width), so they are
# consumed silently rather than treated as unsupported/unknown.
_C0_NOOP_BYTES = frozenset(
    {
        commands.HT,
        commands.FF,
        commands.CAN,
        commands.SO,
        commands.SI,
    }
)


class Parser:
    """Stateful, incremental ESC/POS parser.

    Call `feed(chunk)` for every chunk of bytes received from the
    transport; it returns the list of render ops decoded from the chunk
    (plus any previously buffered partial command that this chunk
    completes). Partial commands at the end of a chunk are buffered
    internally until more data arrives.
    """

    def __init__(
        self,
        implemented_codepages: Optional[Set[int]] = None,
        diagnostics_sink: Optional[Callable[[Diagnostic], None]] = None,
    ) -> None:
        self.state = PrinterState()
        self._buffer = bytearray()
        self._stream_offset = 0  # absolute offset of buffer[0] in the stream
        # Structured diagnostics channel (see core/diagnostics.py): a
        # UI-facing, log-level-independent record of what this parser
        # could not make sense of. Kept separate from the logger.warning
        # calls below, which are unchanged.
        self._diagnostics: List[Diagnostic] = []
        self._diagnostics_sink = diagnostics_sink
        # Per-op byte-offset channel (see take_op_offsets()): one entry per
        # op returned by feed(), used by the composition root to compute
        # exact per-receipt byte counts instead of approximating from the
        # raw chunk size. Purely additive bookkeeping -- it never affects
        # feed()'s return value or any parsing decision.
        self._op_offsets: List[int] = []
        # Code page ids this simulated printer's firmware actually has a
        # table for. Real cheap thermal printers only implement a subset
        # of the ids a profile might claim to support; requesting one
        # outside this set must not be accepted (see `_set_code_table`).
        # Defaults to everything this simulator knows how to decode.
        self._implemented_codepages = (
            implemented_codepages
            if implemented_codepages is not None
            else set(commands.CODEPAGE_MAP)
        )
        # Transient QR configuration, held between GS ( k sub-commands.
        self._qr_model = 0
        self._qr_size = 0
        self._qr_error_correction = 0

    def feed(self, data: bytes) -> List[object]:
        """Feed a chunk of raw bytes and return the ops decoded from it."""
        # Snapshot how much was already buffered from a previous, still-
        # incomplete command before extending with this chunk: needed so
        # take_op_offsets() can tell how much of *this* chunk's own bytes
        # each op consumed, even when it also consumes carried-over bytes.
        buffer_len_before = len(self._buffer)
        chunk_len = len(data)

        self._buffer.extend(data)
        ops: List[object] = []
        consumed_in_call = 0

        while self._buffer:
            result = self._try_parse_one(bytes(self._buffer))
            if result is _NEED_MORE:
                break
            consumed, new_ops = result
            del self._buffer[:consumed]
            self._stream_offset += consumed
            consumed_in_call += consumed
            ops.extend(new_ops)
            if new_ops:
                # Offset within *this* chunk (0..chunk_len]: bytes carried
                # over from a previous incomplete command are excluded from
                # the lower end, and the result is capped at chunk_len so a
                # command that also consumes carried-over bytes is never
                # attributed more of this chunk than it actually received.
                local_offset = min(max(consumed_in_call - buffer_len_before, 0), chunk_len)
                self._op_offsets.extend([local_offset] * len(new_ops))

        return ops

    def take_op_offsets(self) -> List[int]:
        """Return and clear the per-op byte offsets recorded since the last
        call, one entry per op returned by feed() (see feed()'s
        `consumed_in_call`/`local_offset` bookkeeping above).

        This is the draining counterpart to take_diagnostics(): a caller
        (typically the composition root, right after each feed()) drains
        this list and passes it to render.receipts.split_into_receipts()
        (via the byte_offsets it already accepts) to compute the exact
        number of bytes each receipt was built from, instead of
        approximating from the raw chunk size.

        Each offset is relative to the start of the feed() call that
        produced the corresponding op -- not an absolute stream position
        -- so it must be drained after every feed() call, exactly like
        take_diagnostics(). The caller is responsible for translating it
        into whatever running/absolute counter it maintains (e.g. by
        adding its own cumulative byte total from before that call).
        """
        offsets = self._op_offsets
        self._op_offsets = []
        return offsets

    def take_diagnostics(self) -> List[Diagnostic]:
        """Return and clear the diagnostics collected so far.

        This is the polling counterpart to `diagnostics_sink`: a caller
        (typically the composition root, right after each `feed()`)
        drains this list and forwards it to the UI. Draining rather than
        just reading avoids double-reporting the same diagnostic on a
        later call.
        """
        diagnostics = self._diagnostics
        self._diagnostics = []
        return diagnostics

    # -- dispatch -----------------------------------------------------

    def _try_parse_one(
        self, buf: bytes
    ) -> Optional[Tuple[int, List[object]]]:
        b = buf[0]

        if b == commands.LF:
            return 1, [LineFeedOp()]
        if b == commands.ESC:
            return self._parse_esc(buf)
        if b == commands.GS:
            return self._parse_gs(buf)
        if b == commands.DLE:
            return self._parse_dle(buf)
        if b == commands.FS:
            return self._parse_fs(buf)
        if b == commands.CR:
            # Hosts (including esc_pos_utils_plus over print_bluetooth_thermal)
            # commonly emit CRLF pairs, but the thermal head already returns
            # to the left margin and feeds a line on LF alone. Rendering
            # '\r' as a literal glyph would corrupt text, and emitting a
            # second line feed for it would double every blank line, so it
            # carries no op of its own and is simply consumed.
            return 1, []
        if b in _C0_NOOP_BYTES:
            logger.debug(
                "recognized no-op control byte 0x%02X at offset %d, consuming "
                "without effect",
                b,
                self._stream_offset,
            )
            return 1, []
        if b < 0x20:
            # Any other C0 control byte (NUL, ENQ, EOT, DC2, SYN, ...) has no
            # standalone meaning in this profile. It must never be allowed to
            # fall through to _parse_text, where it would silently become an
            # invisible or garbled glyph in rendered output; treat it like
            # any other unrecognized command instead.
            return self._skip_unknown(buf, 1)

        return self._parse_text(buf)

    def _parse_text(self, buf: bytes) -> Tuple[int, List[object]]:
        # Every C0 control byte (0x00-0x1F) is dispatched explicitly above,
        # so a text run only ever needs to stop at the next one of those;
        # everything else (including the full 0x80-0xFF range used by
        # single-byte code pages) is real printable content.
        end = 1
        while end < len(buf) and buf[end] >= 0x20:
            end += 1
        codec = commands.CODEPAGE_MAP.get(self.state.code_table, "cp437")
        raw = buf[:end]
        text = raw.decode(codec, errors="replace")
        text = self._apply_international_charset(raw, text)
        return end, [TextOp(text=text, style=self.state.snapshot(), raw=raw)]

    def _apply_international_charset(self, raw: bytes, text: str) -> str:
        """Re-map the 12 ESC R "international" positions in `text`
        according to `self.state.charset` (see `core.charsets`).

        This runs right after code page decoding, on the same text run,
        because both are just different views of "how do we turn bytes
        the sender already sent into the glyph a real print head would
        strike" -- `ESC t` picks the table, `ESC R` patches 12 positions
        of it. Keeping both here means `render/` never has to know this
        feature exists; it only ever sees the final decoded string.

        `raw` and `text` are assumed index-aligned: true for every codec
        in `commands.CODEPAGE_MAP` today, since none of them decode a
        byte below 0x80 (the only bytes ESC R ever substitutes) into
        anything but a single character at the same position.
        """
        if self.state.charset == 0:
            return text  # USA: charsets.INTERNATIONAL_CHARSETS[0] is a
            # no-op table anyway, but skip the loop for the common case.
        chars: Optional[List[str]] = None
        for i, byte in enumerate(raw):
            if i >= len(text) or byte not in charsets.SUBSTITUTION_POSITIONS:
                continue
            replacement = charsets.resolve_substitution(self.state.charset, byte)
            if replacement is None:
                continue
            if chars is None:
                chars = list(text)
            chars[i] = replacement
        return text if chars is None else "".join(chars)

    # -- DLE (real-time status queries) --------------------------------

    def _parse_dle(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        if len(buf) < 2:
            return _NEED_MORE
        if buf[1] != commands.EOT:
            return self._skip_unknown(buf, 1)
        if len(buf) < 3:
            return _NEED_MORE
        # DLE EOT n: real-time status query. The transport used by the
        # mobile app (print_bluetooth_thermal) is write-only and never
        # reads a response, so this is parsed only to stay in sync with
        # the stream and then discarded.
        logger.debug(
            "discarded DLE EOT status query at offset %d", self._stream_offset
        )
        return 3, []

    # -- FS (Kanji-mode and related) commands ----------------------------

    def _parse_fs(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        if len(buf) < 2:
            return _NEED_MORE
        sub = buf[1]

        if sub == 0x2E:  # FS . - cancel Kanji character mode
            self.state.kanji_mode = False
            return 2, []

        if sub == 0x26:  # FS & - select Kanji character mode
            self.state.kanji_mode = True
            return 2, []

        if sub in (0x43, 0x57, 0x21, 0x2D):
            # FS C n (Kanji code system), FS W n (quad-size mode),
            # FS ! n (Kanji print mode), FS - n (Kanji underline mode).
            # Kanji rendering itself is out of scope for this simulator, so
            # these are consumed and discarded to keep the stream in sync.
            if len(buf) < 3:
                return _NEED_MORE
            return 3, []

        if sub == 0x53:  # FS S n1 n2 - Kanji character spacing
            if len(buf) < 4:
                return _NEED_MORE
            return 4, []

        return self._skip_unknown(buf, 2)

    # -- ESC commands ---------------------------------------------------

    def _parse_esc(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        if len(buf) < 2:
            return _NEED_MORE
        cmd = buf[1]

        if cmd == 0x40:  # ESC @ - initialize
            self.state.reset()
            return 2, [InitOp()]

        if cmd == 0x61:  # ESC a n - align
            return self._need(buf, 3, lambda n: self._set_align(n))

        if cmd == 0x45:  # ESC E n - bold
            return self._need(buf, 3, lambda n: self._set_bold(n))

        if cmd == 0x2D:  # ESC - n - underline
            return self._need(buf, 3, lambda n: self._set_underline(n))

        if cmd == 0x4D:  # ESC M n - font
            return self._need(buf, 3, lambda n: self._set_font(n))

        if cmd == 0x21:  # ESC ! n - combined print mode bitmask
            return self._need(buf, 3, lambda n: self._set_print_mode(n))

        if cmd == 0x74:  # ESC t n - code table
            return self._need(buf, 3, lambda n: self._set_code_table(n))

        if cmd == 0x52:  # ESC R n - international charset
            return self._need(buf, 3, lambda n: self._set_charset(n))

        if cmd == 0x64:  # ESC d n - feed n lines
            if len(buf) < 3:
                return _NEED_MORE
            return 3, [FeedOp(lines=buf[2])]

        if cmd == 0x65:  # ESC e n - reverse feed n lines
            if len(buf) < 3:
                return _NEED_MORE
            return 3, [ReverseFeedOp(lines=buf[2])]

        if cmd == 0x32:  # ESC 2 - default line spacing
            self.state.line_spacing = None
            return 2, [LineSpacingOp(dots=None)]

        if cmd == 0x33:  # ESC 3 n - set line spacing
            if len(buf) < 3:
                return _NEED_MORE
            n = buf[2]
            self.state.line_spacing = n
            return 3, [LineSpacingOp(dots=n)]

        if cmd == 0x7B:  # ESC { n - upside down
            return self._need(
                buf, 3, lambda n: setattr(self.state, "upside_down", bool(n))
            )

        if cmd == 0x56:  # ESC V n - rotate 90
            return self._need(
                buf, 3, lambda n: setattr(self.state, "rotate90", bool(n & 1))
            )

        if cmd == 0x70:  # ESC p m t1 t2 - cash drawer
            if len(buf) < 5:
                return _NEED_MORE
            m, t1, t2 = buf[2], buf[3], buf[4]
            return 5, [CashDrawerOp(pin=m, on_time=t1, off_time=t2)]

        if cmd == 0x2A:  # ESC * m nL nH d... - column bit image
            return self._parse_bit_image(buf)

        if cmd == 0x24:  # ESC $ nL nH - set absolute horizontal print position
            return self._need_word(buf, lambda pos: setattr(self.state, "h_pos_dots", pos))

        if cmd == 0x5C:  # ESC \ nL nH - set relative horizontal print position (signed)
            return self._need_word(buf, self._apply_relative_h_pos)

        return self._skip_unknown(buf, 2)

    def _need(self, buf: bytes, total: int, apply) -> Optional[Tuple[int, List[object]]]:
        """Helper for `ESC x n`-shaped style commands: read n, apply it,
        and emit a StyleChangeOp with the resulting state snapshot."""
        if len(buf) < total:
            return _NEED_MORE
        n = buf[total - 1]
        apply(n)
        return total, [StyleChangeOp(style=self.state.snapshot())]

    def _need_word(self, buf: bytes, apply) -> Optional[Tuple[int, List[object]]]:
        """Helper for `ESC/GS x nL nH`-shaped commands: read a little-endian
        16-bit word (nL + nH * 256), apply it, and emit a StyleChangeOp
        with the resulting state snapshot."""
        if len(buf) < 4:
            return _NEED_MORE
        nl, nh = buf[2], buf[3]
        apply(nl + nh * 256)
        return 4, [StyleChangeOp(style=self.state.snapshot())]

    def _apply_relative_h_pos(self, raw: int) -> None:
        """ESC \\'s argument is a signed 16-bit two's complement value: a
        raw word of 0x8000-0xFFFF represents a negative delta (move
        left). The resulting position is clamped at 0 -- a real print
        head cannot move left of the left margin."""
        delta = raw - 0x10000 if raw >= 0x8000 else raw
        self.state.h_pos_dots = max(0, self.state.h_pos_dots + delta)

    def _set_align(self, n: int) -> None:
        self.state.align = {0: "left", 1: "center", 2: "right"}.get(n, "left")

    def _set_bold(self, n: int) -> None:
        self.state.bold = bool(n & 0x01)

    def _set_underline(self, n: int) -> None:
        # Some senders use ASCII '0'/'1'/'2' (0x30-0x32) instead of 0-2.
        if n in (0x30, 0x31, 0x32):
            n -= 0x30
        self.state.underline = n if n in (0, 1, 2) else 0

    def _set_font(self, n: int) -> None:
        self.state.font = "B" if n & 0x01 else "A"

    def _set_code_table(self, n: int) -> None:
        # Two independent things can make `n` unusable, and they must not
        # be conflated into one diagnostic:
        #  - `n` is not in CODEPAGE_MAP at all: this simulator has no
        #    codec for it, period. That is *our* limitation, so it is
        #    reported here, unconditionally, regardless of
        #    `_implemented_codepages` -- a caller could even have
        #    explicitly claimed to "implement" an id we still can't
        #    decode.
        #  - `n` is in CODEPAGE_MAP but outside `_implemented_codepages`:
        #    handled below, unchanged from before -- this models the
        #    modeled printer's own hardware limitation, already
        #    diagnosed via logger.warning.
        if n not in commands.CODEPAGE_MAP:
            diagnostic = Diagnostic(
                offset=self._stream_offset,
                raw_bytes=bytes([commands.ESC, 0x74, n]),
                reason=(
                    f"ESC t: code page {n} is not a table this simulator "
                    "knows how to decode; rendered text uses a guessed "
                    "fallback codec and should not be trusted"
                ),
                severity="warning",
            )
            self._diagnostics.append(diagnostic)
            if self._diagnostics_sink is not None:
                self._diagnostics_sink(diagnostic)

        # Real firmware silently ignores a request for a table it does not
        # physically carry and stays on whatever table was already active
        # (factory default CP437 after a reset). This does not "fix"
        # anything the sender did -- it models the printer's own
        # limitation, independent of whatever bytes it later receives.
        if n not in self._implemented_codepages:
            logger.warning(
                "ESC t: code page %d is not implemented by this printer "
                "profile, falling back to CP437 (0)",
                n,
            )
            n = 0
        self.state.code_table = n

    def _set_charset(self, n: int) -> None:
        # `state.charset` always records exactly what the sender asked
        # for -- unlike `_set_code_table`, this never rewrites `n` to a
        # fallback value, because the fallback here is purely a
        # rendering decision (see `_apply_international_charset`), not
        # a hardware limitation being modeled. When `n` has no sourced
        # substitution table (see `core.charsets`), that rendering
        # fallback is silent by design (never invents glyphs), so a
        # diagnostic is raised here instead, once per selection, to
        # make sure it is not silently trusted.
        self.state.charset = n
        if not charsets.is_verified(n):
            diagnostic = Diagnostic(
                offset=self._stream_offset,
                raw_bytes=bytes([commands.ESC, 0x52, n]),
                reason=(
                    f"ESC R: international charset {n} has no sourced "
                    "substitution table in this simulator; rendering "
                    "falls back to unmodified US/ASCII glyphs at the 12 "
                    "positions it would otherwise change"
                ),
                severity="warning",
            )
            self._diagnostics.append(diagnostic)
            if self._diagnostics_sink is not None:
                self._diagnostics_sink(diagnostic)

    def _set_print_mode(self, n: int) -> None:
        # ESC ! bitmask (Epson-compatible): bit0=font B, bit3=emphasized,
        # bit4=double height, bit5=double width, bit7=underline.
        self.state.font = "B" if n & 0x01 else "A"
        self.state.bold = bool(n & 0x08)
        self.state.height_mult = 2 if n & 0x10 else 1
        self.state.width_mult = 2 if n & 0x20 else 1
        self.state.underline = 1 if n & 0x80 else 0

    def _parse_bit_image(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        if len(buf) < 5:
            return _NEED_MORE
        mode, nl, nh = buf[2], buf[3], buf[4]
        columns = nl + nh * 256
        bytes_per_column = 3 if mode in (32, 33) else 1
        data_len = columns * bytes_per_column
        total = 5 + data_len
        if len(buf) < total:
            return _NEED_MORE
        data = buf[5:total]
        height = bytes_per_column * 8
        x_offset = self.state.left_margin_dots + self.state.h_pos_dots
        return total, [
            BitImageOp(mode=mode, width=columns, height=height, data=data, x_offset=x_offset)
        ]

    # -- GS commands ------------------------------------------------------

    def _parse_gs(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        if len(buf) < 2:
            return _NEED_MORE
        cmd = buf[1]

        if cmd == 0x21:  # GS ! n - character size
            return self._need(buf, 3, lambda n: self._set_char_size(n))

        if cmd == 0x42:  # GS B n - reverse
            return self._need(buf, 3, lambda n: setattr(self.state, "reverse", bool(n)))

        if cmd == 0x56:  # GS V - paper cut
            return self._parse_cut(buf)

        if cmd == 0x76:  # GS v 0 - raster bit image
            return self._parse_raster(buf)

        if cmd == 0x6B:  # GS k - barcode
            return self._parse_barcode(buf)

        if cmd == 0x68:  # GS h n - barcode height
            return self._need(buf, 3, lambda n: setattr(self.state.barcode, "height", n))

        if cmd == 0x77:  # GS w n - barcode module width
            return self._need(buf, 3, lambda n: setattr(self.state.barcode, "width", n))

        if cmd == 0x66:  # GS f n - barcode HRI font
            return self._need(buf, 3, lambda n: setattr(self.state.barcode, "font", n))

        if cmd == 0x48:  # GS H n - barcode HRI position
            return self._need(
                buf, 3, lambda n: setattr(self.state.barcode, "hri_position", n)
            )

        if cmd == 0x28:  # GS ( k - QR code (and other "( k" functions)
            return self._parse_gs_paren_k(buf)

        if cmd == 0x4C:  # GS L nL nH - set left margin
            return self._need_word(buf, lambda n: setattr(self.state, "left_margin_dots", n))

        if cmd == 0x57:  # GS W nL nH - set printing area width
            return self._need_word(
                buf, lambda n: setattr(self.state, "print_area_width_dots", n)
            )

        return self._skip_unknown(buf, 2)

    def _set_char_size(self, n: int) -> None:
        self.state.width_mult = ((n >> 4) & 0x0F) + 1
        self.state.height_mult = (n & 0x0F) + 1

    def _parse_cut(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        if len(buf) < 3:
            return _NEED_MORE
        m = buf[2]
        if m in (0, 48):
            return 3, [CutOp(mode="full", feed_lines=0)]
        if m in (1, 49):
            return 3, [CutOp(mode="partial", feed_lines=0)]
        if m in (65, 66):
            if len(buf) < 4:
                return _NEED_MORE
            feed_lines = buf[3]
            mode = "full" if m == 65 else "partial"
            return 4, [CutOp(mode=mode, feed_lines=feed_lines)]
        logger.warning(
            "GS V: unexpected cut mode byte 0x%02X at offset %d, treating as full cut",
            m,
            self._stream_offset,
        )
        return 3, [CutOp(mode="full", feed_lines=0)]

    def _parse_raster(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        # GS v 0 m xL xH yL yH d1...dk
        if len(buf) < 3:
            return _NEED_MORE
        if buf[2] != 0x30:
            return self._skip_unknown(buf, 2)
        if len(buf) < 8:
            return _NEED_MORE
        xl, xh, yl, yh = buf[4], buf[5], buf[6], buf[7]
        width_bytes = xl + xh * 256
        height = yl + yh * 256
        data_len = width_bytes * height
        total = 8 + data_len
        if len(buf) < total:
            return _NEED_MORE
        data = buf[8:total]
        x_offset = self.state.left_margin_dots + self.state.h_pos_dots
        return total, [
            RasterImageOp(width=width_bytes * 8, height=height, data=data, x_offset=x_offset)
        ]

    def _parse_barcode(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        if len(buf) < 3:
            return _NEED_MORE
        m = buf[2]

        if m in _BARCODE_LEGACY:
            nul_index = buf.find(commands.NUL, 3)
            if nul_index == -1:
                return _NEED_MORE
            data = buf[3:nul_index]
            total = nul_index + 1
            return total, [BarcodeOp(symbology=_BARCODE_LEGACY[m], data=bytes(data))]

        if m in _BARCODE_PREFIXED:
            if len(buf) < 4:
                return _NEED_MORE
            n = buf[3]
            total = 4 + n
            if len(buf) < total:
                return _NEED_MORE
            data = buf[4:total]
            return total, [BarcodeOp(symbology=_BARCODE_PREFIXED[m], data=bytes(data))]

        return self._skip_unknown(buf, 3)

    def _parse_gs_paren_k(self, buf: bytes) -> Optional[Tuple[int, List[object]]]:
        # GS ( k pL pH cn fn [params...]
        if len(buf) < 5:
            return _NEED_MORE
        pl, ph = buf[3], buf[4]
        payload_len = pl + ph * 256
        total = 5 + payload_len
        if len(buf) < total:
            return _NEED_MORE
        payload = buf[5:total]
        if len(payload) < 2:
            return total, []
        cn, fn = payload[0], payload[1]
        params = payload[2:]

        if cn != 0x31:  # only the "1" (symbol storage) function class is supported
            logger.warning(
                "GS ( k: unsupported function class 0x%02X at offset %d, skipping",
                cn,
                self._stream_offset,
            )
            return total, []

        ops: List[object] = []
        if fn == 0x41 and len(params) >= 1:  # select QR model
            self._qr_model = params[0]
        elif fn == 0x43 and len(params) >= 1:  # module size
            self._qr_size = params[0]
        elif fn == 0x45 and len(params) >= 1:  # error correction level
            self._qr_error_correction = params[0]
        elif fn == 0x50 and len(params) >= 1:  # store data (params[0] is fixed m=0x30)
            data = bytes(params[1:])
            ops.append(QrStoreOp(data=data))
        elif fn == 0x51:  # print stored symbol
            ops.append(
                QrPrintOp(
                    model=self._qr_model,
                    size=self._qr_size,
                    error_correction=self._qr_error_correction,
                )
            )
        else:
            logger.warning(
                "GS ( k: unknown or malformed fn 0x%02X at offset %d, skipping",
                fn,
                self._stream_offset,
            )

        return total, ops

    # -- unknown/garbage recovery ---------------------------------------

    def _skip_unknown(self, buf: bytes, count: int) -> Tuple[int, List[object]]:
        count = min(count, len(buf))
        skipped = buf[:count]
        logger.warning(
            "unknown or unsupported command at offset %d, skipping bytes: %s",
            self._stream_offset,
            skipped.hex(" "),
        )
        diagnostic = Diagnostic(
            offset=self._stream_offset,
            raw_bytes=bytes(skipped),
            reason="unknown or unsupported command",
            severity="warning",
        )
        self._diagnostics.append(diagnostic)
        if self._diagnostics_sink is not None:
            self._diagnostics_sink(diagnostic)
        return count, []
