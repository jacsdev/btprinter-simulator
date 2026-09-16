"""Tests for core.diagnostics / Parser's structured diagnostics channel.

This is a UI-facing concern that must work independently of logging
configuration: `logger.warning` calls in `core/parser.py` are unchanged
(see tests/test_parser.py for those), this file only covers the new
structured channel that lets a person looking at the viewer window (not
the console) see what the parser could not make sense of.
"""

import logging

from core.diagnostics import Diagnostic
from core.parser import Parser


def test_clean_stream_yields_no_diagnostics():
    parser = Parser()

    parser.feed(b"HELLO\x0a")

    assert parser.take_diagnostics() == []


def test_unknown_command_yields_exactly_one_diagnostic_with_offset_and_hex():
    parser = Parser()

    parser.feed(bytes([0x1B, 0xFF]) + b"OK\x0a")

    diagnostics = parser.take_diagnostics()
    assert len(diagnostics) == 1
    diag = diagnostics[0]
    assert isinstance(diag, Diagnostic)
    assert diag.offset == 0
    assert diag.raw_bytes == bytes([0x1B, 0xFF])
    assert "unknown" in diag.reason.lower() or "unsupported" in diag.reason.lower()
    assert diag.severity == "warning"


def test_multiple_unknown_commands_accumulate_across_feed_calls():
    parser = Parser()

    parser.feed(bytes([0x00]))
    parser.feed(bytes([0x04]))

    diagnostics = parser.take_diagnostics()
    assert len(diagnostics) == 2
    assert diagnostics[0].offset == 0
    assert diagnostics[1].offset == 1


def test_take_diagnostics_drains_the_list():
    parser = Parser()
    parser.feed(bytes([0x00]))

    first = parser.take_diagnostics()
    second = parser.take_diagnostics()

    assert len(first) == 1
    assert second == []


def test_diagnostics_sink_callback_receives_each_diagnostic_immediately():
    received = []
    parser = Parser(diagnostics_sink=received.append)

    parser.feed(bytes([0x00]))

    assert len(received) == 1
    assert received[0].offset == 0
    # Still available via take_diagnostics() as well -- the sink is an
    # additional notification path, not a replacement for the list.
    assert len(parser.take_diagnostics()) == 1


def test_existing_logger_warning_call_is_unchanged_alongside_diagnostics(caplog):
    parser = Parser()
    with caplog.at_level(logging.WARNING):
        parser.feed(bytes([0x1B, 0xFF]))

    assert any("unknown" in record.message.lower() for record in caplog.records)
    assert len(parser.take_diagnostics()) == 1


def test_diagnostic_source_defaults_to_none_and_is_settable():
    # `source` is never set by Parser itself (see core/diagnostics.py) --
    # only main.py attaches it, to attribute a diagnostic to a specific
    # opened file rather than the live transport.
    parser = Parser()
    parser.feed(bytes([0x00]))

    diag = parser.take_diagnostics()[0]
    assert diag.source is None

    tagged = Diagnostic(
        offset=diag.offset,
        raw_bytes=diag.raw_bytes,
        reason=diag.reason,
        severity=diag.severity,
        source="opened file: capture.bin",
    )
    assert tagged.source == "opened file: capture.bin"


def test_parsing_behaviour_is_unchanged_alongside_diagnostics():
    # Regression guard: adding the diagnostics channel must not alter
    # what valid, well-formed commands parse into.
    from core.ops import TextOp

    parser = Parser()
    ops = parser.feed(bytes([0x1B, 0xFF]) + b"OK\x0a")

    assert any(isinstance(op, TextOp) and op.text == "OK" for op in ops)
