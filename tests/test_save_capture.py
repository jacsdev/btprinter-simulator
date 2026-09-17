"""Tests for the "Save capture..." feature: the viewer always buffers the
raw received bytes in memory (bounded, see Viewer._RAW_CAPTURE_MAX_BYTES),
and the toolbar button exports what has already arrived -- either the
selected receipt's slice of it, or the whole session -- without ever
having decided in advance that a capture would be wanted.

The Tk save dialog itself is mocked (`render.viewer.filedialog.
asksaveasfilename`); these tests exercise the underlying selection and
buffering logic, not whether a Tk dialog "looks right".
"""

import os
import tkinter as tk

import pytest
from core.parser import Parser
from core.state import PrinterState
from core.ops import CutOp, LineFeedOp, TextOp
from main import replay_file
from render.viewer import Viewer
from tools.send_sample import build_sample_ticket

pytestmark = pytest.mark.skipif(
    os.environ.get("BTPRINTER_SKIP_GUI_TESTS") == "1",
    reason="GUI tests disabled via BTPRINTER_SKIP_GUI_TESTS",
)


@pytest.fixture(scope="module")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


def _make_viewer(tk_root):
    return Viewer(width_dots=384, master=tk_root)


def _feed_chunks(viewer, parser, data, chunk_size):
    """Drive a Viewer exactly the way main.py's on_data callback does:
    feed `data` through `parser` in `chunk_size`-sized pieces, forwarding
    each raw chunk to append_raw_bytes() and the resulting ops to
    update_ops(), both keyed to the same per-op byte offsets."""
    for i in range(0, len(data), chunk_size):
        chunk = data[i : i + chunk_size]
        ops = parser.feed(chunk)
        offsets = parser.take_op_offsets()
        viewer.append_raw_bytes(chunk)
        viewer.update_ops(ops, len(chunk), offsets)


# -- round trip: the strongest test in this slice ---------------------------


def test_round_trip_saved_whole_session_replays_to_the_same_ops(tk_root, tmp_path):
    ticket = build_sample_ticket()
    viewer = _make_viewer(tk_root)
    try:
        parser = Parser()
        _feed_chunks(viewer, parser, ticket, chunk_size=7)

        target = viewer.get_capture_target()
        out_path = tmp_path / "roundtrip.bin"
        viewer.save_capture(str(out_path), target.data)

        reference_ops = Parser().feed(ticket)
        replayed = replay_file(str(out_path))

        assert replayed.ops == reference_ops
        assert len(replayed.ops) > 0
    finally:
        viewer.destroy()


def test_round_trip_saved_selected_receipt_replays_to_that_receipts_ops(tk_root, tmp_path):
    # Two receipts in one session; saving the selected one and replaying
    # it must reproduce exactly that receipt's ops, not the whole session.
    ticket = b"FIRST\x0a" + bytes([0x1D, 0x56, 0x00]) + b"SECOND\x0a"
    viewer = _make_viewer(tk_root)
    try:
        parser = Parser()
        _feed_chunks(viewer, parser, ticket, chunk_size=3)

        receipts = viewer.get_receipts()
        assert len(receipts) == 2

        viewer._history_listbox.selection_set(0)
        target = viewer.get_capture_target()
        out_path = tmp_path / "receipt1.bin"
        viewer.save_capture(str(out_path), target.data)

        replayed = replay_file(str(out_path))
        expected_ops = Parser().feed(ticket[: len(target.data)])

        assert replayed.ops == expected_ops
        assert any(isinstance(op, CutOp) for op in replayed.ops)
    finally:
        viewer.destroy()


# -- byte-identical capture --------------------------------------------------


def test_whole_session_capture_is_byte_identical_to_bytes_received(tk_root, tmp_path):
    data = bytes(range(256)) * 4 + b"\x0a"
    viewer = _make_viewer(tk_root)
    try:
        parser = Parser()
        _feed_chunks(viewer, parser, data, chunk_size=11)

        target = viewer.get_capture_target()

        assert target.data == data
    finally:
        viewer.destroy()


# -- per-receipt selection ----------------------------------------------------


def test_selected_receipt_capture_matches_only_that_receipts_bytes(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        data = b"AAA\x0a" + bytes([0x1D, 0x56, 0x00]) + b"BBBBB\x0a"
        parser = Parser()
        _feed_chunks(viewer, parser, data, chunk_size=4)

        receipts = viewer.get_receipts()
        assert len(receipts) == 2

        viewer._history_listbox.selection_set(1)
        target = viewer.get_capture_target()

        assert len(target.data) == receipts[1].byte_count
        assert target.data == data[len(data) - receipts[1].byte_count :]
    finally:
        viewer.destroy()


def test_no_selection_capture_matches_the_status_bars_bytes_received(tk_root):
    from render.viewer import TransportStatus

    viewer = Viewer(width_dots=384, master=tk_root)
    try:
        status = TransportStatus(
            transport="tcp", endpoint="port 9100", width_mm=58, codepages_label="all"
        )
        data = b"HELLO WORLD\x0a"
        parser = Parser()

        chunk_size = 5
        for i in range(0, len(data), chunk_size):
            chunk = data[i : i + chunk_size]
            ops = parser.feed(chunk)
            offsets = parser.take_op_offsets()
            viewer.append_raw_bytes(chunk)
            viewer.update_ops(ops, len(chunk), offsets)
            status = status.add_bytes(len(chunk)).add_ops(len(ops))

        viewer.update_status(status)
        viewer._history_listbox.selection_clear(0, tk.END)

        target = viewer.get_capture_target()

        assert len(target.data) == status.bytes_received
    finally:
        viewer.destroy()


# -- cancelling the dialog ----------------------------------------------------


def test_save_capture_click_writes_nothing_when_dialog_is_cancelled(tk_root, monkeypatch, tmp_path):
    viewer = _make_viewer(tk_root)
    try:
        viewer.append_raw_bytes(b"HELLO\x0a")
        viewer.update_ops([TextOp(text="HELLO", style=PrinterState()), LineFeedOp()], 6, [6, 6])

        monkeypatch.setattr("render.viewer.filedialog.asksaveasfilename", lambda **kw: "")

        would_be_path = tmp_path / "should_not_exist.bin"
        viewer._on_save_capture_click()

        assert not would_be_path.exists()
        assert list(tmp_path.iterdir()) == []
    finally:
        viewer.destroy()


def test_save_capture_click_writes_the_chosen_path_when_confirmed(tk_root, monkeypatch, tmp_path):
    viewer = _make_viewer(tk_root)
    try:
        viewer.append_raw_bytes(b"HELLO\x0a")
        viewer.update_ops([TextOp(text="HELLO", style=PrinterState()), LineFeedOp()], 6, [6, 6])

        out_path = tmp_path / "chosen.bin"
        monkeypatch.setattr("render.viewer.filedialog.asksaveasfilename", lambda **kw: str(out_path))

        viewer._on_save_capture_click()

        assert out_path.read_bytes() == b"HELLO\x0a"
    finally:
        viewer.destroy()


# -- buffer cap ---------------------------------------------------------------


def test_capture_buffer_keeps_only_the_most_recent_bytes_past_the_cap(tk_root, monkeypatch):
    monkeypatch.setattr("render.viewer._RAW_CAPTURE_MAX_BYTES", 100)
    viewer = _make_viewer(tk_root)
    try:
        first = bytes([0xAA]) * 60
        second = bytes([0xBB]) * 60  # total 120 > cap of 100

        viewer.append_raw_bytes(first)
        viewer.append_raw_bytes(second)

        target = viewer.get_capture_target()

        assert len(target.data) == 100
        assert target.data == (first + second)[-100:]
        assert target.is_partial is True
    finally:
        viewer.destroy()


def test_capture_buffer_is_not_marked_partial_when_under_the_cap(tk_root, monkeypatch):
    monkeypatch.setattr("render.viewer._RAW_CAPTURE_MAX_BYTES", 1000)
    viewer = _make_viewer(tk_root)
    try:
        viewer.append_raw_bytes(b"small amount of data")

        target = viewer.get_capture_target()

        assert target.is_partial is False
    finally:
        viewer.destroy()


# -- clear() resets the capture buffer, like it does ops/receipts -----------


def test_clear_resets_the_capture_buffer(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.append_raw_bytes(b"OLD DATA\x0a")
        viewer.update_ops([TextOp(text="OLD DATA", style=PrinterState()), LineFeedOp()], 9, [9, 9])

        viewer.clear()

        target = viewer.get_capture_target()
        assert target.data == b""
    finally:
        viewer.destroy()


# -- label disambiguation: which of the two is about to be saved -----------


def test_capture_target_label_mentions_receipt_when_one_is_selected(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        data = b"A\x0a" + bytes([0x1D, 0x56, 0x00])
        parser = Parser()
        ops = parser.feed(data)
        offsets = parser.take_op_offsets()
        viewer.append_raw_bytes(data)
        viewer.update_ops(ops, len(data), offsets)

        viewer._history_listbox.selection_set(0)
        target = viewer.get_capture_target()

        assert "receipt" in target.label.lower()
    finally:
        viewer.destroy()


def test_capture_target_label_mentions_session_when_nothing_is_selected(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.append_raw_bytes(b"A\x0a")
        viewer.update_ops([TextOp(text="A", style=PrinterState()), LineFeedOp()], 2, [2, 2])

        target = viewer.get_capture_target()

        assert "session" in target.label.lower()
    finally:
        viewer.destroy()
