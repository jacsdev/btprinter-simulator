"""Tests for the printer style state (core.state.PrinterState)."""

from core.state import BarcodeConfig, PrinterState


def test_default_state_has_expected_power_on_values():
    state = PrinterState()

    assert state.align == "left"
    assert state.bold is False
    assert state.underline == 0
    assert state.font == "A"
    assert state.width_mult == 1
    assert state.height_mult == 1
    assert state.reverse is False
    assert state.upside_down is False
    assert state.rotate90 is False
    assert state.line_spacing is None
    assert state.code_table == 0
    assert state.charset == 0
    assert isinstance(state.barcode, BarcodeConfig)
    assert state.h_pos_dots == 0
    assert state.left_margin_dots == 0
    assert state.print_area_width_dots is None


def test_reset_restores_power_on_defaults_after_mutation():
    state = PrinterState()
    state.align = "center"
    state.bold = True
    state.underline = 2
    state.font = "B"
    state.width_mult = 4
    state.height_mult = 3
    state.reverse = True
    state.upside_down = True
    state.rotate90 = True
    state.line_spacing = 40
    state.code_table = 5
    state.charset = 2
    state.barcode.height = 200
    state.h_pos_dots = 100
    state.left_margin_dots = 20
    state.print_area_width_dots = 300

    state.reset()

    assert state == PrinterState()


def test_snapshot_is_independent_copy():
    state = PrinterState()
    snap = state.snapshot()
    state.bold = True
    state.barcode.height = 999

    assert snap.bold is False
    assert snap.barcode.height != 999
