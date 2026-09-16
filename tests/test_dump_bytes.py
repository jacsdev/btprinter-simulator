"""Tests for --dump-bytes: capturing the raw received byte stream to a
file so a real print becomes a permanent regression fixture.
"""

import main


def test_byte_dumper_writes_exactly_the_bytes_received_byte_for_byte(tmp_path):
    path = tmp_path / "capture.bin"
    dump = main._make_byte_dumper(str(path))

    dump(b"HELLO")
    dump(bytes([0x1B, 0x40]))
    dump(b"\x0a")
    dump.close()

    assert path.read_bytes() == b"HELLO" + bytes([0x1B, 0x40]) + b"\x0a"


def test_byte_dumper_appends_to_an_existing_file_instead_of_overwriting(tmp_path):
    path = tmp_path / "capture.bin"
    path.write_bytes(b"PRIOR")

    dump = main._make_byte_dumper(str(path))
    dump(b"NEW")
    dump.close()

    assert path.read_bytes() == b"PRIORNEW"


def test_byte_dumper_flushes_after_every_write_without_closing(tmp_path):
    path = tmp_path / "capture.bin"
    dump = main._make_byte_dumper(str(path))

    dump(b"X")

    # Flushed immediately -- a crash before dump.close() must not lose
    # the capture. Read back via an independent handle without closing.
    assert path.read_bytes() == b"X"
    dump.close()


def test_byte_dumper_never_raises_on_empty_chunks(tmp_path):
    path = tmp_path / "capture.bin"
    dump = main._make_byte_dumper(str(path))

    dump(b"")
    dump(b"A")
    dump.close()

    assert path.read_bytes() == b"A"
