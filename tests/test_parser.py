"""Tests for core.parser.Parser: ESC/POS byte stream -> render ops.

Covers, per the slice-1 spec: every supported command decoding to the
expected op, style state transitions, graceful skipping of unknown/garbage
bytes without raising, commands split across feed() calls, and the GS v 0
raster pixel-dimension math.
"""

import logging

import pytest

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
from core.parser import Parser


def make_parser():
    return Parser()


# ---------------------------------------------------------------------------
# Plain text and LF
# ---------------------------------------------------------------------------


def test_plain_text_then_lf_emits_text_and_line_feed_ops():
    parser = make_parser()

    ops = parser.feed(b"HELLO\x0a")

    assert len(ops) == 2
    assert isinstance(ops[0], TextOp)
    assert ops[0].text == "HELLO"
    assert isinstance(ops[1], LineFeedOp)


def test_text_op_carries_current_style_snapshot():
    parser = make_parser()

    ops = parser.feed(b"\x1b\x45\x01ABC")

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "ABC"
    assert text_op.style.bold is True


# ---------------------------------------------------------------------------
# ESC @ - initialize / reset
# ---------------------------------------------------------------------------


def test_esc_at_emits_init_op_and_resets_style():
    parser = make_parser()
    parser.feed(b"\x1b\x45\x01")  # turn bold on
    assert parser.state.bold is True

    ops = parser.feed(b"\x1b\x40")

    assert any(isinstance(op, InitOp) for op in ops)
    assert parser.state.bold is False


# ---------------------------------------------------------------------------
# ESC a - align
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [(0, "left"), (1, "center"), (2, "right")],
)
def test_esc_a_sets_alignment(n, expected):
    parser = make_parser()

    ops = parser.feed(bytes([0x1B, 0x61, n]))

    assert parser.state.align == expected
    style_op = next(op for op in ops if isinstance(op, StyleChangeOp))
    assert style_op.style.align == expected


# ---------------------------------------------------------------------------
# ESC E - bold
# ---------------------------------------------------------------------------


def test_esc_e_toggles_bold_on_and_off():
    parser = make_parser()

    parser.feed(bytes([0x1B, 0x45, 1]))
    assert parser.state.bold is True

    parser.feed(bytes([0x1B, 0x45, 0]))
    assert parser.state.bold is False


# ---------------------------------------------------------------------------
# ESC - - underline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n,expected", [(0, 0), (1, 1), (2, 2)])
def test_esc_minus_sets_underline_level(n, expected):
    parser = make_parser()

    parser.feed(bytes([0x1B, 0x2D, n]))

    assert parser.state.underline == expected


# ---------------------------------------------------------------------------
# ESC M - font selection
# ---------------------------------------------------------------------------


def test_esc_m_selects_font_a_and_b():
    parser = make_parser()

    parser.feed(bytes([0x1B, 0x4D, 1]))
    assert parser.state.font == "B"

    parser.feed(bytes([0x1B, 0x4D, 0]))
    assert parser.state.font == "A"


# ---------------------------------------------------------------------------
# ESC ! - combined print mode bitmask
# ---------------------------------------------------------------------------


def test_esc_bang_bitmask_sets_font_bold_size_underline():
    parser = make_parser()
    # bit0=font B, bit3=bold, bit4=double height, bit5=double width, bit7=underline
    n = 0b1011_1001
    parser.feed(bytes([0x1B, 0x21, n]))

    assert parser.state.font == "B"
    assert parser.state.bold is True
    assert parser.state.height_mult == 2
    assert parser.state.width_mult == 2
    assert parser.state.underline == 1


def test_esc_bang_zero_restores_plain_mode():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x21, 0xFF]))

    parser.feed(bytes([0x1B, 0x21, 0x00]))

    assert parser.state.font == "A"
    assert parser.state.bold is False
    assert parser.state.height_mult == 1
    assert parser.state.width_mult == 1
    assert parser.state.underline == 0


# ---------------------------------------------------------------------------
# ESC t / ESC R - code table / international charset
# ---------------------------------------------------------------------------


def test_esc_t_sets_code_table():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x74, 3]))
    assert parser.state.code_table == 3


def test_esc_r_sets_charset():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x52, 2]))
    assert parser.state.charset == 2


# ---------------------------------------------------------------------------
# ESC t - code page selection and mojibake fidelity
#
# `ESC t n` never changes how the sender encoded its bytes; it only tells
# the printer which table to use when interpreting bytes it already
# received. When the two disagree, a real printer produces mojibake on
# paper, and the simulator must reproduce that mojibake rather than
# silently correct it.
# ---------------------------------------------------------------------------


def test_esc_t_declared_codepage_mismatching_sender_encoding_reproduces_mojibake():
    parser = make_parser()
    original = "São João Conceição"
    # The sender (app side) encodes as latin1, independent of whatever
    # table the printer is later told to use.
    latin1_bytes = original.encode("latin1")
    # cp850 disagrees with latin1 for the accented bytes involved here, so
    # decoding under the declared table produces real mojibake.
    expected = latin1_bytes.decode("cp850", errors="replace")

    parser.feed(bytes([0x1B, 0x74, 2]))  # ESC t 2 -> CP850
    ops = parser.feed(latin1_bytes)

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == expected
    assert text_op.text != original


def test_esc_t_selects_implemented_codepage_cp1252():
    parser = Parser(implemented_codepages={0, 16})
    text = "café"
    cp1252_bytes = text.encode("cp1252")

    parser.feed(bytes([0x1B, 0x74, 16]))
    ops = parser.feed(cp1252_bytes)

    assert parser.state.code_table == 16
    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == text


def test_esc_t_unimplemented_codepage_falls_back_to_cp437(caplog):
    parser = Parser(implemented_codepages={0, 2})  # 16 (cp1252) not implemented

    with caplog.at_level(logging.WARNING):
        parser.feed(bytes([0x1B, 0x74, 16]))

    assert parser.state.code_table == 0
    assert any("16" in record.message for record in caplog.records)

    # Subsequent text still decodes with the fallback table (CP437), not
    # the unimplemented one that was requested.
    ops = parser.feed(bytes([0x80]))  # 0x80 is 'Ç' under CP437
    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == bytes([0x80]).decode("cp437")


def test_esc_at_resets_code_table_to_cp437():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x74, 2]))
    assert parser.state.code_table == 2

    parser.feed(bytes([0x1B, 0x40]))

    assert parser.state.code_table == 0


# ---------------------------------------------------------------------------
# ESC d / ESC e - feed / reverse feed
# ---------------------------------------------------------------------------


def test_esc_d_emits_feed_op():
    parser = make_parser()
    ops = parser.feed(bytes([0x1B, 0x64, 5]))
    assert ops == [FeedOp(lines=5)]


def test_esc_e_emits_reverse_feed_op():
    parser = make_parser()
    ops = parser.feed(bytes([0x1B, 0x65, 2]))
    assert ops == [ReverseFeedOp(lines=2)]


# ---------------------------------------------------------------------------
# ESC 2 / ESC 3 - line spacing
# ---------------------------------------------------------------------------


def test_esc_2_sets_default_line_spacing():
    parser = make_parser()
    ops = parser.feed(bytes([0x1B, 0x32]))
    assert ops == [LineSpacingOp(dots=None)]
    assert parser.state.line_spacing is None


def test_esc_3_sets_explicit_line_spacing():
    parser = make_parser()
    ops = parser.feed(bytes([0x1B, 0x33, 40]))
    assert ops == [LineSpacingOp(dots=40)]
    assert parser.state.line_spacing == 40


# ---------------------------------------------------------------------------
# ESC { - upside down / ESC V - rotate 90
# ---------------------------------------------------------------------------


def test_esc_brace_toggles_upside_down():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x7B, 1]))
    assert parser.state.upside_down is True
    parser.feed(bytes([0x1B, 0x7B, 0]))
    assert parser.state.upside_down is False


def test_esc_v_toggles_rotate90():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x56, 1]))
    assert parser.state.rotate90 is True
    parser.feed(bytes([0x1B, 0x56, 0]))
    assert parser.state.rotate90 is False


# ---------------------------------------------------------------------------
# ESC p - cash drawer
# ---------------------------------------------------------------------------


def test_esc_p_emits_cash_drawer_event():
    parser = make_parser()
    ops = parser.feed(bytes([0x1B, 0x70, 0, 50, 200]))
    assert ops == [CashDrawerOp(pin=0, on_time=50, off_time=200)]


# ---------------------------------------------------------------------------
# ESC * - column bit image
# ---------------------------------------------------------------------------


def test_esc_star_single_density_bit_image():
    parser = make_parser()
    columns = 2
    data = bytes([0xFF, 0x00])  # 1 byte per column at single density
    ops = parser.feed(bytes([0x1B, 0x2A, 0, columns, 0]) + data)

    assert ops == [BitImageOp(mode=0, width=2, height=8, data=data)]


def test_esc_star_double_density_bit_image():
    parser = make_parser()
    columns = 1
    data = bytes([0x01, 0x02, 0x03])  # 3 bytes per column at double density
    ops = parser.feed(bytes([0x1B, 0x2A, 33, columns, 0]) + data)

    assert ops == [BitImageOp(mode=33, width=1, height=24, data=data)]


# ---------------------------------------------------------------------------
# ESC $ / ESC \ - horizontal print position (absolute / relative)
# GS L / GS W - left margin / printing area width
#
# Diagnosed from a real Flutter print job (esc_pos_utils_plus): the parser
# did not know ESC $, so "1b 24 00 00" produced 3 diagnostics each time it
# appeared (the ESC $ pair itself, plus each of its 2 argument bytes
# falling through as unrecognized bytes of their own). 9 occurrences in the
# real capture x 3 diagnostics = exactly the 27 warnings observed.
# ---------------------------------------------------------------------------


def test_esc_dollar_zero_position_yields_zero_diagnostics_the_real_log_regression():
    parser = make_parser()

    ops = parser.feed(bytes([0x1B, 0x24, 0x00, 0x00]) + b"OK\x0a")

    assert parser.take_diagnostics() == []
    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "OK"


def test_esc_dollar_nine_times_interleaved_with_text_yields_zero_diagnostics():
    # Replays the exact repeating pattern from the real capture: ESC $ 00 00
    # interleaved with text, 9 times.
    parser = make_parser()

    for i in range(9):
        parser.feed(bytes([0x1B, 0x24, 0x00, 0x00]))
        parser.feed(f"line {i}\x0a".encode("ascii"))

    assert parser.take_diagnostics() == []


def test_esc_dollar_sets_absolute_horizontal_position():
    parser = make_parser()

    ops = parser.feed(bytes([0x1B, 0x24, 100, 0]))

    assert parser.state.h_pos_dots == 100
    assert ops == [StyleChangeOp(style=parser.state.snapshot())]


def test_esc_dollar_position_uses_little_endian_two_byte_word():
    parser = make_parser()

    parser.feed(bytes([0x1B, 0x24, 0x2C, 0x01]))  # 0x012C = 300

    assert parser.state.h_pos_dots == 300


def test_esc_dollar_split_across_two_feed_calls():
    parser = make_parser()
    command = bytes([0x1B, 0x24, 100, 0])

    ops1 = parser.feed(command[:2])
    assert ops1 == []
    ops2 = parser.feed(command[2:3])
    assert ops2 == []
    ops3 = parser.feed(command[3:])

    assert parser.state.h_pos_dots == 100
    assert len(ops3) == 1


def test_esc_backslash_moves_position_right_with_positive_delta():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x24, 50, 0]))  # start at 50

    parser.feed(bytes([0x1B, 0x5C, 20, 0]))  # +20

    assert parser.state.h_pos_dots == 70


def test_esc_backslash_negative_two_s_complement_moves_left():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x24, 50, 0]))  # start at 50

    # -20 as a signed 16-bit two's complement word: 0xFFEC = 65516
    raw = (-20) & 0xFFFF
    nl, nh = raw & 0xFF, (raw >> 8) & 0xFF
    parser.feed(bytes([0x1B, 0x5C, nl, nh]))

    assert parser.state.h_pos_dots == 30


def test_esc_backslash_negative_delta_clamps_at_zero():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x24, 10, 0]))  # start at 10

    raw = (-100) & 0xFFFF
    nl, nh = raw & 0xFF, (raw >> 8) & 0xFF
    parser.feed(bytes([0x1B, 0x5C, nl, nh]))

    assert parser.state.h_pos_dots == 0


def test_esc_backslash_split_across_two_feed_calls():
    parser = make_parser()
    command = bytes([0x1B, 0x5C, 20, 0])

    ops1 = parser.feed(command[:1])
    assert ops1 == []
    ops2 = parser.feed(command[1:3])
    assert ops2 == []
    ops3 = parser.feed(command[3:])

    assert parser.state.h_pos_dots == 20
    assert len(ops3) == 1


def test_esc_at_resets_print_position_family_to_defaults():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x24, 100, 0]))  # h_pos
    parser.feed(bytes([0x1D, 0x4C, 10, 0]))  # left margin
    parser.feed(bytes([0x1D, 0x57, 200, 0]))  # print area width
    assert parser.state.h_pos_dots == 100
    assert parser.state.left_margin_dots == 10
    assert parser.state.print_area_width_dots == 200

    parser.feed(bytes([0x1B, 0x40]))

    assert parser.state.h_pos_dots == 0
    assert parser.state.left_margin_dots == 0
    assert parser.state.print_area_width_dots is None


def test_gs_l_sets_left_margin():
    parser = make_parser()

    ops = parser.feed(bytes([0x1D, 0x4C, 24, 0]))

    assert parser.state.left_margin_dots == 24
    assert ops == [StyleChangeOp(style=parser.state.snapshot())]


def test_gs_l_split_across_two_feed_calls():
    parser = make_parser()
    command = bytes([0x1D, 0x4C, 24, 0])

    ops1 = parser.feed(command[:2])
    assert ops1 == []
    ops2 = parser.feed(command[2:])

    assert parser.state.left_margin_dots == 24
    assert len(ops2) == 1


def test_gs_w_sets_printing_area_width():
    parser = make_parser()

    ops = parser.feed(bytes([0x1D, 0x57, 0x90, 0x01]))  # 0x0190 = 400

    assert parser.state.print_area_width_dots == 400
    assert ops == [StyleChangeOp(style=parser.state.snapshot())]


def test_gs_w_split_across_two_feed_calls():
    parser = make_parser()
    command = bytes([0x1D, 0x57, 0x90, 0x01])

    ops1 = parser.feed(command[:3])
    assert ops1 == []
    ops2 = parser.feed(command[3:])

    assert parser.state.print_area_width_dots == 400
    assert len(ops2) == 1


def test_raster_image_captures_x_offset_from_left_margin_and_h_pos():
    parser = make_parser()
    parser.feed(bytes([0x1D, 0x4C, 10, 0]))  # left margin = 10
    parser.feed(bytes([0x1B, 0x24, 5, 0]))  # h_pos = 5
    width_bytes = 1
    height = 1
    data = bytes([0xFF])
    header = bytes([0x1D, 0x76, 0x30, 0, width_bytes, 0, height, 0])

    ops = parser.feed(header + data)

    raster_op = next(op for op in ops if isinstance(op, RasterImageOp))
    assert raster_op.x_offset == 15


def test_bit_image_captures_x_offset_from_left_margin_and_h_pos():
    parser = make_parser()
    parser.feed(bytes([0x1B, 0x24, 30, 0]))  # h_pos = 30
    columns = 1
    data = bytes([0xFF])

    ops = parser.feed(bytes([0x1B, 0x2A, 0, columns, 0]) + data)

    bit_image_op = next(op for op in ops if isinstance(op, BitImageOp))
    assert bit_image_op.x_offset == 30


# ---------------------------------------------------------------------------
# GS ! - character size multipliers
# ---------------------------------------------------------------------------


def test_gs_bang_sets_width_and_height_multipliers():
    parser = make_parser()
    # high nibble = width-1, low nibble = height-1 -> width x4, height x3
    n = (0x3 << 4) | 0x2
    parser.feed(bytes([0x1D, 0x21, n]))

    assert parser.state.width_mult == 4
    assert parser.state.height_mult == 3


# ---------------------------------------------------------------------------
# GS B - reverse (white on black)
# ---------------------------------------------------------------------------


def test_gs_b_toggles_reverse():
    parser = make_parser()
    parser.feed(bytes([0x1D, 0x42, 1]))
    assert parser.state.reverse is True
    parser.feed(bytes([0x1D, 0x42, 0]))
    assert parser.state.reverse is False


# ---------------------------------------------------------------------------
# GS V - paper cut
# ---------------------------------------------------------------------------


def test_gs_v_full_cut_short_form():
    parser = make_parser()
    ops = parser.feed(bytes([0x1D, 0x56, 0]))
    assert ops == [CutOp(mode="full", feed_lines=0)]


def test_gs_v_partial_cut_short_form():
    parser = make_parser()
    ops = parser.feed(bytes([0x1D, 0x56, 1]))
    assert ops == [CutOp(mode="partial", feed_lines=0)]


def test_gs_v_full_cut_with_feed_form():
    parser = make_parser()
    ops = parser.feed(bytes([0x1D, 0x56, 65, 10]))
    assert ops == [CutOp(mode="full", feed_lines=10)]


def test_gs_v_partial_cut_with_feed_form():
    parser = make_parser()
    ops = parser.feed(bytes([0x1D, 0x56, 66, 5]))
    assert ops == [CutOp(mode="partial", feed_lines=5)]


# ---------------------------------------------------------------------------
# GS v 0 - raster bit image
# ---------------------------------------------------------------------------


def test_gs_v0_raster_image_pixel_dimensions():
    parser = make_parser()
    width_bytes = 2  # 16 pixels wide
    height = 3  # 3 rows
    data = bytes(width_bytes * height)  # all-zero raster data
    header = bytes([0x1D, 0x76, 0x30, 0, width_bytes, 0, height, 0])

    ops = parser.feed(header + data)

    assert ops == [RasterImageOp(width=16, height=3, data=data)]


def test_gs_v0_raster_image_split_across_two_feed_calls():
    parser = make_parser()
    width_bytes = 1
    height = 1
    data = bytes([0b10101010])
    header = bytes([0x1D, 0x76, 0x30, 0, width_bytes, 0, height, 0])
    full = header + data

    ops1 = parser.feed(full[:5])
    assert ops1 == []

    ops2 = parser.feed(full[5:])
    assert ops2 == [RasterImageOp(width=8, height=1, data=data)]


# ---------------------------------------------------------------------------
# GS k - barcode (legacy NUL-terminated and length-prefixed forms)
# ---------------------------------------------------------------------------


def test_gs_k_legacy_nul_terminated_form():
    parser = make_parser()
    payload = b"123456789012"
    ops = parser.feed(bytes([0x1D, 0x6B, 2]) + payload + b"\x00")  # m=2 -> EAN13

    assert ops == [BarcodeOp(symbology="EAN13", data=payload)]


def test_gs_k_length_prefixed_form():
    parser = make_parser()
    payload = b"HELLO123"
    ops = parser.feed(
        bytes([0x1D, 0x6B, 73, len(payload)]) + payload
    )  # m=73 -> CODE128

    assert ops == [BarcodeOp(symbology="CODE128", data=payload)]


def test_gs_k_split_across_feed_calls_waits_for_terminator():
    parser = make_parser()
    payload = b"9998887776"
    full = bytes([0x1D, 0x6B, 4]) + payload + b"\x00"  # m=4 -> CODE39

    ops1 = parser.feed(full[:6])
    assert ops1 == []

    ops2 = parser.feed(full[6:])
    assert ops2 == [BarcodeOp(symbology="CODE39", data=payload)]


# ---------------------------------------------------------------------------
# GS h / GS w / GS f / GS H - barcode configuration
# ---------------------------------------------------------------------------


def test_gs_barcode_config_commands_update_state():
    parser = make_parser()
    parser.feed(bytes([0x1D, 0x68, 120]))
    parser.feed(bytes([0x1D, 0x77, 4]))
    parser.feed(bytes([0x1D, 0x66, 1]))
    parser.feed(bytes([0x1D, 0x48, 2]))

    assert parser.state.barcode.height == 120
    assert parser.state.barcode.width == 4
    assert parser.state.barcode.font == 1
    assert parser.state.barcode.hri_position == 2


# ---------------------------------------------------------------------------
# GS ( k - QR code
# ---------------------------------------------------------------------------


def test_qr_store_and_print_sequence():
    parser = make_parser()
    data = b"https://softtur.example/ticket/123"

    # fn=65: select model 2
    parser.feed(bytes([0x1D, 0x28, 0x6B, 0x04, 0x00, 0x31, 0x41, 0x32, 0x00]))
    # fn=67: module size 6
    parser.feed(bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, 0x06]))
    # fn=69: error correction level M (0x31 = "1")
    parser.feed(bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x45, 0x31]))
    # fn=80: store data, prefixed with fixed m=0x30
    store_payload = bytes([0x31, 0x50, 0x30]) + data
    plen = len(store_payload)
    store_ops = parser.feed(
        bytes([0x1D, 0x28, 0x6B, plen & 0xFF, (plen >> 8) & 0xFF]) + store_payload
    )
    # fn=81: print, fixed m=0x30
    print_ops = parser.feed(
        bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x51, 0x30])
    )

    assert store_ops == [QrStoreOp(data=data)]
    assert print_ops == [QrPrintOp(model=0x32, size=6, error_correction=0x31)]


# ---------------------------------------------------------------------------
# DLE EOT - real-time status query, parsed and discarded
# ---------------------------------------------------------------------------


def test_dle_eot_status_query_is_discarded_without_ops():
    parser = make_parser()
    ops = parser.feed(bytes([0x10, 0x04, 1]))
    assert ops == []


# ---------------------------------------------------------------------------
# Robustness: unknown/garbage bytes never raise
# ---------------------------------------------------------------------------


def test_unknown_esc_command_is_skipped_and_logged(caplog):
    parser = make_parser()
    with caplog.at_level(logging.WARNING):
        ops = parser.feed(bytes([0x1B, 0xFF]) + b"OK\x0a")

    assert any(isinstance(op, TextOp) and op.text == "OK" for op in ops)
    assert any("unknown" in record.message.lower() for record in caplog.records)


def test_random_garbage_bytes_never_raise():
    parser = make_parser()
    garbage = bytes([0x1B, 0x99, 0x1D, 0xEE, 0xEE, 0x7F, 0x01])

    # Must not raise, regardless of how nonsensical the input is.
    ops = parser.feed(garbage)

    assert isinstance(ops, list)


def test_malformed_stream_recovers_and_keeps_parsing_valid_commands():
    parser = make_parser()
    # 0x1B 0xAB is an unrecognized ESC command (skipped as 2 unknown bytes);
    # the stray 0xCD is itself a non-control byte and is merged into the
    # following text run, same as any other printable byte would be.
    stream = bytes([0x1B, 0xAB, 0xCD]) + b"HELLO\x0a"

    ops = parser.feed(stream)

    combined_text = "".join(op.text for op in ops if isinstance(op, TextOp))
    assert "HELLO" in combined_text
    assert any(isinstance(op, LineFeedOp) for op in ops)


# ---------------------------------------------------------------------------
# Streaming: a command split across two feed() calls
# ---------------------------------------------------------------------------


def test_command_split_across_two_feed_calls():
    parser = make_parser()
    command = bytes([0x1B, 0x61, 1])  # ESC a 1 -> align center

    ops1 = parser.feed(command[:1])
    assert ops1 == []
    ops2 = parser.feed(command[1:2])
    assert ops2 == []
    ops3 = parser.feed(command[2:])

    assert parser.state.align == "center"
    assert len(ops3) == 1


def test_text_and_command_split_across_feed_calls():
    parser = make_parser()

    ops1 = parser.feed(b"AB")
    ops2 = parser.feed(b"C\x0a")

    combined_text = "".join(op.text for op in ops1 + ops2 if isinstance(op, TextOp))
    assert combined_text == "ABC"
    assert any(isinstance(op, LineFeedOp) for op in ops2)


# ---------------------------------------------------------------------------
# FS (0x1C) - Kanji-mode and Kanji-adjacent commands
#
# Real Flutter print jobs (esc_pos_utils_plus 2.0.4) emit `FS .` (cancel
# Kanji character mode) before styled text blocks. Before this fix, FS was
# not dispatched at all, so both bytes fell through to `_parse_text` and
# were rendered as literal (garbled) text -- corrupting content and, on a
# 32-column receipt, pushing 2 real characters past the column budget.
# ---------------------------------------------------------------------------


def test_fs_dot_cancel_kanji_mode_is_consumed_with_no_leading_garbage_in_text():
    parser = make_parser()

    ops = parser.feed(bytes([0x1C, 0x2E]) + b"LISTA DE EMBARQUE\n")

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "LISTA DE EMBARQUE"
    assert "\x1c" not in text_op.text
    assert "." != text_op.text[0]


def test_fs_dot_does_not_steal_columns_from_a_32_char_line():
    parser = make_parser()
    line = "A" * 32  # exact 32-column budget (58mm, Font A)

    ops = parser.feed(bytes([0x1C, 0x2E]) + line.encode("ascii") + b"\n")

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == line
    assert len(text_op.text) == 32


def test_fs_dot_sets_kanji_mode_false_on_state():
    parser = make_parser()
    parser.state.kanji_mode = True

    ops = parser.feed(bytes([0x1C, 0x2E]))

    assert ops == []
    assert parser.state.kanji_mode is False


def test_fs_ampersand_selects_kanji_mode_on_state():
    parser = make_parser()

    ops = parser.feed(bytes([0x1C, 0x26]))

    assert ops == []
    assert parser.state.kanji_mode is True


@pytest.mark.parametrize(
    "sub_byte",
    [0x43, 0x57, 0x21, 0x2D],
    ids=["FS_C", "FS_W", "FS_bang", "FS_minus"],
)
def test_fs_three_byte_kanji_commands_are_consumed_and_ignored(sub_byte):
    parser = make_parser()

    ops = parser.feed(bytes([0x1C, sub_byte, 0x01]) + b"OK\n")

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "OK"


def test_fs_s_kanji_spacing_is_consumed_and_ignored():
    parser = make_parser()

    ops = parser.feed(bytes([0x1C, 0x53, 0x02, 0x02]) + b"OK\n")

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "OK"


def test_fs_unknown_subcommand_is_skipped_and_logged(caplog):
    parser = make_parser()
    with caplog.at_level(logging.WARNING):
        ops = parser.feed(bytes([0x1C, 0xFF]) + b"OK\n")

    assert any(isinstance(op, TextOp) and op.text == "OK" for op in ops)
    assert any("unknown" in record.message.lower() for record in caplog.records)


def test_fs_dot_split_across_two_feed_calls():
    parser = make_parser()
    parser.state.kanji_mode = True

    ops1 = parser.feed(bytes([0x1C]))
    assert ops1 == []

    ops2 = parser.feed(bytes([0x2E]) + b"OK\n")

    assert parser.state.kanji_mode is False
    text_op = next(op for op in ops2 if isinstance(op, TextOp))
    assert text_op.text == "OK"


def test_fs_three_byte_command_split_across_two_feed_calls():
    parser = make_parser()

    ops1 = parser.feed(bytes([0x1C, 0x43]))
    assert ops1 == []

    ops2 = parser.feed(bytes([0x01]) + b"OK\n")

    text_op = next(op for op in ops2 if isinstance(op, TextOp))
    assert text_op.text == "OK"


def test_fs_s_command_split_across_two_feed_calls():
    parser = make_parser()

    ops1 = parser.feed(bytes([0x1C, 0x53, 0x02]))
    assert ops1 == []

    ops2 = parser.feed(bytes([0x02]) + b"OK\n")

    text_op = next(op for op in ops2 if isinstance(op, TextOp))
    assert text_op.text == "OK"


# ---------------------------------------------------------------------------
# Other C0 control bytes reaching the top-level dispatcher.
#
# Any byte in 0x00-0x1F is a control code, never legitimate printable
# content, so none of them may fall through to `_parse_text` and become a
# visible glyph. Each one is either handled with defined ESC/POS semantics,
# consumed as a recognized-but-inert no-op, or treated as an unrecognized
# command (skipped and logged), matching the same "unknown commands are
# skipped and logged, real text is rendered as-is" rule used elsewhere.
# ---------------------------------------------------------------------------


def test_cr_is_consumed_without_emitting_text_or_extra_line_feed():
    parser = make_parser()

    ops = parser.feed(b"HELLO\r\n")

    assert len(ops) == 2
    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "HELLO"
    assert "\r" not in text_op.text
    assert sum(1 for op in ops if isinstance(op, LineFeedOp)) == 1


@pytest.mark.parametrize(
    "byte,name",
    [
        (0x09, "HT"),
        (0x0C, "FF"),
        (0x18, "CAN"),
        (0x0E, "SO"),
        (0x0F, "SI"),
    ],
)
def test_recognized_noop_control_bytes_are_consumed_without_visible_text(byte, name):
    parser = make_parser()

    ops = parser.feed(bytes([byte]) + b"OK\n")

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "OK"
    assert chr(byte) not in text_op.text


@pytest.mark.parametrize(
    "byte,name",
    [
        (0x00, "NUL"),
        (0x04, "EOT"),
        (0x05, "ENQ"),
        (0x12, "DC2"),
        (0x16, "SYN"),
    ],
)
def test_unrecognized_standalone_c0_bytes_are_skipped_and_logged(byte, name, caplog):
    parser = make_parser()

    with caplog.at_level(logging.WARNING):
        ops = parser.feed(bytes([byte]) + b"OK\n")

    text_op = next(op for op in ops if isinstance(op, TextOp))
    assert text_op.text == "OK"
    assert chr(byte) not in text_op.text
    assert any("unknown" in record.message.lower() for record in caplog.records)
