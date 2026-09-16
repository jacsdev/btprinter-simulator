"""Tests for the ESC/POS command byte constants."""

from core import commands


def test_control_byte_constants():
    assert commands.LF == 0x0A
    assert commands.ESC == 0x1B
    assert commands.GS == 0x1D
    assert commands.DLE == 0x10
    assert commands.EOT == 0x04
    assert commands.NUL == 0x00
