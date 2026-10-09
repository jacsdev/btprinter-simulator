"""Tests for Parser real-time status responses (DLE EOT n).

v1 scope: the printer is permanently healthy (online, paper present,
cover closed, no error), so every supported status query (n in 1..4) is
answered with the same "all clear" byte 0x12. The parser never writes to
a transport itself -- it only PRODUCES response bytes as pure data, which
the composition root drains via `take_status_responses()` and writes back
to the connected peer.
"""

from __future__ import annotations

from core.parser import Parser


def make_parser() -> Parser:
    return Parser()


def test_dle_eot_1_returns_no_ops_and_one_healthy_status_byte():
    parser = make_parser()

    ops = parser.feed(b"\x10\x04\x01")

    assert ops == []
    assert parser.take_status_responses() == [b"\x12"]


def test_each_supported_n_answers_with_the_same_healthy_status_byte():
    for n in (1, 2, 3, 4):
        parser = make_parser()

        ops = parser.feed(bytes([0x10, 0x04, n]))

        assert ops == []
        assert parser.take_status_responses() == [b"\x12"]


def test_unsupported_n_consumes_three_bytes_without_a_response():
    for n in (0, 5):
        parser = make_parser()

        ops = parser.feed(bytes([0x10, 0x04, n]))

        assert ops == []
        assert parser.take_status_responses() == []


def test_dle_eot_split_across_feeds_buffers_until_n_arrives():
    parser = make_parser()

    assert parser.feed(b"\x10\x04") == []
    assert parser.take_status_responses() == []

    ops = parser.feed(b"\x01")

    assert ops == []
    assert parser.take_status_responses() == [b"\x12"]


def test_dle_not_followed_by_eot_skips_without_a_response():
    parser = make_parser()

    ops = parser.feed(b"\x10\x00")

    assert ops == []
    assert parser.take_status_responses() == []


def test_plain_text_and_other_commands_produce_no_responses():
    parser = make_parser()

    ops = parser.feed(b"Hello\n")

    assert ops  # sanity: this really decoded render ops
    assert parser.take_status_responses() == []


def test_take_status_responses_clears_after_draining():
    parser = make_parser()

    parser.feed(b"\x10\x04\x01")

    assert parser.take_status_responses() == [b"\x12"]
    assert parser.take_status_responses() == []
