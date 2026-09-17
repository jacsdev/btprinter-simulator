"""Tests for core.comparative_decode: the inspection-only side view that
decodes the same raw text-run bytes under several candidate code pages
at once, to help a human spot which one the sender actually intended.

This is explicitly NOT part of the render pipeline: it must never be
able to raise on bytes a candidate codec cannot represent, and it must
never touch parser/render state (see tests/test_viewer.py for the
render-is-unaffected regression guard).
"""

from core.comparative_decode import DEFAULT_CANDIDATE_IDS, decode_under_candidates
from core.commands import CODEPAGE_MAP


def test_decodes_the_same_bytes_under_every_requested_candidate():
    # 0xE9 is a real, representable byte in every candidate below, but
    # decodes to a different glyph under each -- exactly the ambiguity
    # this tool exists to surface.
    raw = bytes([0xE9])

    result = decode_under_candidates(raw, candidate_ids=[0, 2, 3, 16])

    assert result[0] == raw.decode("cp437")
    assert result[2] == raw.decode("cp850")
    assert result[3] == raw.decode("cp860")
    assert result[16] == raw.decode("cp1252")
    # Sanity: at least one candidate must actually disagree with another,
    # otherwise this "which table is right" tool would be pointless.
    assert len({result[0], result[2], result[3], result[16]}) > 1


def test_default_candidates_cover_cp437_cp850_cp860_cp1252_at_minimum():
    assert {0, 2, 3, 16}.issubset(set(DEFAULT_CANDIDATE_IDS))


def test_default_candidates_with_no_explicit_ids_cover_every_known_codepage_map_entry():
    result = decode_under_candidates(b"hello")

    assert set(result.keys()) == set(CODEPAGE_MAP.keys())


def test_unrepresentable_byte_under_a_candidate_codec_does_not_raise():
    # 0x81 has no assigned character in CP1252 (it is one of the
    # "undefined" positions in that table) -- a naive comparison across
    # candidates must not crash the inspection view over it.
    raw = bytes([0x41, 0x81, 0x42])  # "A", undefined-in-cp1252, "B"

    result = decode_under_candidates(raw, candidate_ids=[0, 16])

    assert result[16].startswith("A")
    assert result[16].endswith("B")
    assert "A" in result[16] and "B" in result[16]
    # CP437 has a real assignment for 0x81 ('ü'), so it decodes cleanly.
    assert result[0] == "A\xfcB"


def test_comparative_decode_never_raises_for_any_byte_value_under_any_candidate():
    raw = bytes(range(256))
    # Must complete without raising for every candidate this simulator
    # knows about, regardless of how "hostile" the byte range is. Note:
    # candidates that are multi-byte codecs (e.g. CP932/Shift-JIS, id 1)
    # can legitimately decode this to a *different* length than `raw`
    # (lead/trail byte sequences and replacement runs don't map 1:1), so
    # only single-byte codecs are checked for length preservation.
    result = decode_under_candidates(raw)
    assert len(result) == len(CODEPAGE_MAP)
    for cid, text in result.items():
        assert isinstance(text, str)
        if cid != 1:  # 1 == CP932, the one multi-byte codec in CODEPAGE_MAP
            assert len(text) == len(raw)
