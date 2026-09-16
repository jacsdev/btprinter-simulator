"""Round-trip test for --replay: a dumped byte capture, replayed through
a fresh Parser, must reproduce exactly the ops the live stream produced.
This is the strongest test in the viewer-scrolling/receipts slice.

replay_file() returns a ReplayResult(ops, diagnostics, byte_offsets) --
see main.py's docstring -- rather than a bare list, so every consumer
(the --replay CLI path and the Open button) can forward diagnostics
instead of silently discarding them (the original defect: replaying a
capture with unknown commands showed "No unknown commands").
"""

from pathlib import Path

from core.ops import BitImageOp, CutOp, RasterImageOp
from core.parser import Parser
from main import _make_byte_dumper, replay_file
from render.receipts import split_into_receipts
from tools.send_sample import build_sample_ticket

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_replaying_a_dumped_capture_reproduces_the_original_live_ops(tmp_path):
    ticket = build_sample_ticket()
    path = tmp_path / "capture.bin"

    # Write the capture the same way main.py's on_data callback does:
    # bytes arrive in small, arbitrarily-sized chunks, each one appended
    # to the dump file as it is received. The dump file's *content* is
    # byte-for-byte independent of how it was chunked on the way in
    # (see tests/test_dump_bytes.py), so a fresh replay of it must
    # reproduce exactly the ops a fresh parser gets from the same bytes
    # fed as a single stream -- the same "live" the capture represents.
    dump = _make_byte_dumper(str(path))
    chunk_size = 7
    for i in range(0, len(ticket), chunk_size):
        dump(ticket[i : i + chunk_size])
    dump.close()

    reference_ops = Parser().feed(ticket)
    replayed = replay_file(str(path))

    assert replayed.ops == reference_ops
    assert len(replayed.ops) > 0


def test_replaying_a_dumped_capture_matches_a_single_shot_live_feed_regardless_of_original_chunking(tmp_path):
    # The parser intentionally flushes a text run at the end of whatever
    # chunk it arrived in (see core/parser.py's _parse_text docstring),
    # so a live session that happened to split bytes mid-word produces
    # more, shorter TextOps than one that received the same bytes in a
    # single read. The raw byte dump never records those chunk
    # boundaries -- only main.py's live on_data path sees them -- so
    # what a replay reproduces is a parse of the captured bytes as a
    # single stream, not whatever chunk-dependent fragmentation the
    # original live session happened to have. This test pins that down
    # explicitly, one word split across two chunks.
    ticket = b"HELLO WORLD\x0a"
    path = tmp_path / "capture.bin"
    dump = _make_byte_dumper(str(path))
    dump(ticket[:3])  # "HEL"
    dump(ticket[3:])  # "LO WORLD\n"
    dump.close()

    replayed = replay_file(str(path))
    single_shot_ops = Parser().feed(ticket)

    assert replayed.ops == single_shot_ops


def test_replay_file_honors_implemented_codepages():
    # A codepage-restricted replay must fall back exactly like a live
    # parser configured the same way would.
    ticket = bytes([0x1B, 0x74, 16]) + "café".encode("cp1252")
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(ticket)

        result = replay_file(path, implemented_codepages={0, 2})

        from core.ops import TextOp

        text_op = next(op for op in result.ops if isinstance(op, TextOp))
        # codepage 16 not implemented -> falls back to cp437 (0)
        assert text_op.text == "café".encode("cp1252").decode("cp437", errors="replace")
    finally:
        os.remove(path)


def test_replay_file_returns_empty_result_for_empty_file(tmp_path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")

    result = replay_file(str(path))

    assert result.ops == []
    assert result.diagnostics == []
    assert result.byte_offsets == []


# -- defect 1: replay must forward parser diagnostics, not discard them ----


def test_replay_file_returns_diagnostics_for_unknown_commands(tmp_path):
    # Two genuinely unknown commands (bare NUL and EOT bytes have no
    # standalone meaning -- see core/parser.py's _try_parse_one) mixed in
    # with otherwise well-formed content.
    capture = b"OK" + bytes([0x00]) + b"MORE" + bytes([0x04]) + b"TEXT\x0a"
    path = tmp_path / "capture.bin"
    path.write_bytes(capture)

    result = replay_file(str(path))

    assert len(result.diagnostics) == 2
    assert result.diagnostics[0].offset == 2  # right after "OK"
    assert result.diagnostics[0].raw_bytes == bytes([0x00])
    assert result.diagnostics[1].offset == 7  # right after "OK\x00MORE"
    assert result.diagnostics[1].raw_bytes == bytes([0x04])


def test_replay_file_returns_no_diagnostics_for_a_clean_capture(tmp_path):
    path = tmp_path / "capture.bin"
    path.write_bytes(b"HELLO\x0a")

    result = replay_file(str(path))

    assert result.diagnostics == []


# -- defect 2: real per-op byte offsets for accurate receipt byte counts ---


def test_replay_file_byte_offsets_match_ops_length_and_sum_to_total_bytes(tmp_path):
    # A capture with a cut mid-stream, so a receipt boundary falls inside
    # the single feed() call replay_file() makes -- exactly the shape of
    # the "545 bytes / 0 bytes" session-history bug.
    capture = b"A\x0a" + bytes([0x1D, 0x56, 0x00]) + b"BB\x0a"
    path = tmp_path / "capture.bin"
    path.write_bytes(capture)

    result = replay_file(str(path))

    assert len(result.byte_offsets) == len(result.ops)
    assert result.byte_offsets[-1] == len(capture)

    from render.receipts import split_into_receipts

    receipts = split_into_receipts(result.ops, byte_offsets=result.byte_offsets)
    assert len(receipts) == 2
    assert sum(r.byte_count for r in receipts) == len(capture)
    assert all(r.byte_count != 0 for r in receipts)


# -- ping-pong.bin: a real, complex 29,293-byte capture from the target
# Flutter app (both image encodings, print-position commands, styles, a
# cut) kept as a permanent regression fixture. Contains no personal
# data -- generic text and a ping-pong graphic. Regression for the
# "29293 bytes received / 17005 bytes in session history" bug: replaying
# it must account for every one of its bytes. ---------------------------


def test_ping_pong_fixture_replays_with_zero_diagnostics_and_exact_byte_accounting():
    path = FIXTURES_DIR / "ping-pong.bin"
    data = path.read_bytes()
    assert len(data) == 29293

    result = replay_file(str(path))

    assert len(result.ops) == 88
    assert result.diagnostics == []
    assert len(result.byte_offsets) == len(result.ops)
    assert result.byte_offsets[-1] == len(data)

    assert sum(1 for op in result.ops if isinstance(op, BitImageOp)) == 13
    assert sum(1 for op in result.ops if isinstance(op, RasterImageOp)) == 1
    assert sum(1 for op in result.ops if isinstance(op, CutOp)) == 1

    receipts = split_into_receipts(result.ops, byte_offsets=result.byte_offsets, total_bytes=len(data))
    assert len(receipts) == 1
    assert receipts[0].byte_count == len(data)
