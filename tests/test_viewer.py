"""Tests for render.viewer.Viewer.

The Tkinter mainloop itself is not exercised here (it blocks forever by
design) — these tests create the root window, drive the non-blocking
parts of the API, and destroy it, which is sufficient to validate the
accumulate-ops -> re-render -> display pipeline without a live GUI
session. See tools/send_sample.py + main.py for the interactive path.
"""

import os
import tkinter as tk
from pathlib import Path

import pytest
from PIL import Image

from core.diagnostics import Diagnostic
from core.ops import CutOp, LineFeedOp, TextOp
from core.parser import Parser
from core.state import PrinterState
from render.viewer import DEFAULT_GEOMETRY, TransportStatus, Viewer

FIXTURES_DIR = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    os.environ.get("BTPRINTER_SKIP_GUI_TESTS") == "1",
    reason="GUI tests disabled via BTPRINTER_SKIP_GUI_TESTS",
)


@pytest.fixture(scope="module")
def tk_root():
    # A single shared Tcl interpreter for every test in this module. Creating
    # and tearing down a fresh Tk() per test was observed to intermittently
    # race on the Microsoft Store Python build's virtualized (MSIX) tcl/tk
    # library files; sharing one root avoids the repeated init/teardown.
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


def _make_viewer(tk_root):
    return Viewer(width_dots=384, master=tk_root)


def test_viewer_starts_with_a_blank_receipt(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        image = viewer.get_current_image()
        assert isinstance(image, Image.Image)
        assert image.width == 384
    finally:
        viewer.destroy()


def test_viewer_update_ops_grows_the_rendered_receipt(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        before = viewer.get_current_image().height

        viewer.update_ops([TextOp(text="HELLO", style=PrinterState()), LineFeedOp()])

        after = viewer.get_current_image().height
        assert after > before
    finally:
        viewer.destroy()


def test_viewer_save_png_writes_a_readable_image(tk_root, tmp_path):
    viewer = _make_viewer(tk_root)
    try:
        viewer.update_ops([TextOp(text="RECEIPT", style=PrinterState()), LineFeedOp()])
        out_path = tmp_path / "receipt.png"

        viewer.save_png(str(out_path))

        assert out_path.exists()
        saved = Image.open(out_path)
        assert saved.width == 384
    finally:
        viewer.destroy()


def _make_status(**overrides) -> TransportStatus:
    defaults = dict(
        transport="rfcomm",
        endpoint="channel 4",
        width_mm=58,
        codepages_label="all",
        sdp_uuid="00001101-0000-1000-8000-00805F9B34FB",
    )
    defaults.update(overrides)
    return TransportStatus(**defaults)


def test_viewer_shows_status_bar_text_from_initial_status(tk_root):
    viewer = Viewer(width_dots=384, master=tk_root, status=_make_status().listening())
    try:
        assert "channel 4" in viewer._status_var.get()
    finally:
        viewer.destroy()


def test_viewer_update_status_refreshes_the_status_bar(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.update_status(_make_status().listening())
        assert "channel 4" in viewer._status_var.get()

        viewer.update_status(_make_status().listening().client_connected(peer="00:11:22:33:44:55"))
        assert "00:11:22:33:44:55" in viewer._status_var.get()
    finally:
        viewer.destroy()


def test_viewer_without_ops_shows_empty_state_text_on_canvas(tk_root):
    viewer = Viewer(width_dots=384, master=tk_root, status=_make_status().listening())
    try:
        canvas_text = viewer._canvas.itemcget(1, "text")
        assert "channel 4" in canvas_text
    finally:
        viewer.destroy()


def test_viewer_update_ops_replaces_empty_state_with_the_receipt(tk_root):
    viewer = Viewer(width_dots=384, master=tk_root, status=_make_status().listening())
    try:
        viewer.update_ops([TextOp(text="HELLO", style=PrinterState()), LineFeedOp()])
        # Canvas now holds an image item, not the placeholder text item.
        items = viewer._canvas.find_withtag("all")
        assert items
        assert viewer._canvas.type(items[0]) == "image"
    finally:
        viewer.destroy()


def test_viewer_set_title_updates_the_window_title(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.set_title("58mm Simulator - RFCOMM (channel 4)")
        assert viewer._root.title() == "58mm Simulator - RFCOMM (channel 4)"
    finally:
        viewer.destroy()


# -- smart auto-scroll (thin widget-layer wiring; the predicate itself is
# tested in isolation in tests/test_scroll_policy.py) ------------------


def test_viewer_autoscrolls_to_bottom_when_previously_at_bottom(tk_root, monkeypatch):
    viewer = _make_viewer(tk_root)
    try:
        monkeypatch.setattr(viewer._canvas, "yview", lambda: (0.0, 1.0))
        moved_to = []
        monkeypatch.setattr(viewer._canvas, "yview_moveto", lambda f: moved_to.append(f))

        viewer.update_ops([TextOp(text="A", style=PrinterState()), LineFeedOp()])

        assert moved_to and moved_to[-1] == 1.0
    finally:
        viewer.destroy()


def test_viewer_does_not_autoscroll_when_user_scrolled_up(tk_root, monkeypatch):
    viewer = _make_viewer(tk_root)
    try:
        monkeypatch.setattr(viewer._canvas, "yview", lambda: (0.0, 0.4))
        moved_to = []
        monkeypatch.setattr(viewer._canvas, "yview_moveto", lambda f: moved_to.append(f))

        viewer.update_ops([TextOp(text="A", style=PrinterState()), LineFeedOp()])

        assert moved_to == []
    finally:
        viewer.destroy()


# -- Clear ("new paper") ------------------------------------------------


def test_viewer_clear_resets_ops_and_receipts_to_the_blank_state(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.update_ops([TextOp(text="A", style=PrinterState()), CutOp(mode="full")])
        assert viewer.get_current_image().height > 1
        assert viewer.get_receipts()

        viewer.clear()

        assert viewer.get_current_image().height == 1
        assert viewer.get_receipts() == []
    finally:
        viewer.destroy()


def test_viewer_clear_resets_diagnostics_counter(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.add_diagnostics([Diagnostic(offset=0, raw_bytes=b"\x00", reason="unknown", severity="warning")])
        assert viewer.get_diagnostics_count() == 1

        viewer.clear()

        assert viewer.get_diagnostics_count() == 0
    finally:
        viewer.destroy()


def test_viewer_clear_does_not_touch_the_status_bar(tk_root):
    # Uses the shared module root. Creating a second Tk() while the fixture's
    # interpreter is alive made this test fail intermittently on Windows.
    viewer = Viewer(width_dots=384, master=tk_root, status=_make_status().listening())
    try:
        text_before = viewer._status_var.get()

        viewer.clear()

        assert viewer._status_var.get() == text_before
    finally:
        viewer.destroy()


def test_viewer_clear_invokes_the_injected_on_clear_callback(tk_root):
    calls = []
    viewer = Viewer(width_dots=384, master=tk_root, on_clear=lambda: calls.append(True))
    try:
        viewer.clear()
        assert calls == [True]
    finally:
        viewer.destroy()


# -- receipts / session history panel ------------------------------------


def test_viewer_update_ops_splits_the_accumulated_ops_into_receipts(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.update_ops([TextOp(text="A", style=PrinterState()), CutOp(mode="full")])
        viewer.update_ops([TextOp(text="B", style=PrinterState())])

        receipts = viewer.get_receipts()

        assert len(receipts) == 2
        assert receipts[0].sequence == 1
        assert receipts[1].sequence == 2
    finally:
        viewer.destroy()


def test_viewer_update_ops_uses_supplied_byte_offsets_for_accurate_per_receipt_counts(tk_root):
    # Regression test for the exact bug observed in the session history
    # panel: a single chunk (e.g. a whole replayed file, fed to the
    # parser in one call) containing two receipts must not attribute all
    # of the chunk's bytes to the first receipt and 0 to the second.
    viewer = _make_viewer(tk_root)
    try:
        ops = [
            TextOp(text="A", style=PrinterState()),
            CutOp(mode="full"),
            TextOp(text="BB", style=PrinterState()),
        ]
        # "A" + CutOp (say 3 raw bytes) = offset 4, then "BB" = offset 6.
        viewer.update_ops(ops, chunk_bytes=6, byte_offsets=[4, 4, 6])

        receipts = viewer.get_receipts()
        assert len(receipts) == 2
        assert receipts[0].byte_count == 4
        assert receipts[1].byte_count == 2
        assert receipts[1].byte_count != 0
    finally:
        viewer.destroy()


def test_viewer_update_ops_byte_offsets_are_cumulative_across_calls(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.update_ops(
            [TextOp(text="A", style=PrinterState()), CutOp(mode="full")],
            chunk_bytes=4,
            byte_offsets=[4, 4],
        )
        viewer.update_ops(
            [TextOp(text="B", style=PrinterState())],
            chunk_bytes=3,
            byte_offsets=[3],
        )

        receipts = viewer.get_receipts()
        assert receipts[0].byte_count == 4
        assert receipts[1].byte_count == 3
    finally:
        viewer.destroy()


def test_viewer_update_ops_rejects_mismatched_byte_offsets_length(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        with pytest.raises(ValueError):
            viewer.update_ops([TextOp(text="A", style=PrinterState())], chunk_bytes=1, byte_offsets=[1, 2])
    finally:
        viewer.destroy()


def test_viewer_update_ops_with_no_new_ops_still_advances_the_byte_total(tk_root):
    # Regression for the exact defect behind the "29293 bytes received /
    # 17005 bytes in session history" bug: a chunk that completes no op
    # at all (the parser is still buffering a partial op) must still be
    # reflected in the session history's byte total once it is fed to
    # update_ops(), not silently dropped.
    viewer = _make_viewer(tk_root)
    try:
        viewer.update_ops([TextOp(text="A", style=PrinterState())], chunk_bytes=1, byte_offsets=[1])
        # A chunk that landed entirely inside a still-incomplete command:
        # zero ops, but the bytes were physically received.
        viewer.update_ops([], chunk_bytes=5, byte_offsets=[])

        receipts = viewer.get_receipts()
        assert len(receipts) == 1
        assert receipts[0].byte_count == 6
    finally:
        viewer.destroy()


# -- byte-accounting invariant: session history must always reconcile
# with the status bar's "Bytes received" (see render.viewer.Viewer.
# update_ops()'s docstring and render.receipts.split_into_receipts()'s
# `total_bytes` parameter) -----------------------------------------------


def _drive_and_assert_reconciled(viewer: Viewer, data: bytes, chunk_size: int) -> None:
    """Feed `data` through a fresh `Parser` in `chunk_size` pieces,
    mirroring main.py's on_data callback: every chunk, even one that
    completes no op, both advances a status-bar-style running byte
    counter and is passed to `viewer.update_ops()`. After every single
    chunk, the sum of the session history's byte counts (which already
    includes the trailing, not-yet-cut receipt) must equal that counter.
    """
    parser = Parser()
    status_bytes = 0
    for start in range(0, len(data), chunk_size):
        chunk = data[start : start + chunk_size]
        ops = parser.feed(chunk)
        byte_offsets = parser.take_op_offsets()
        status_bytes += len(chunk)

        viewer.update_ops(ops, len(chunk), byte_offsets)

        history_total = sum(r.byte_count for r in viewer.get_receipts())
        assert history_total == status_bytes, (
            f"history total {history_total} != status bar bytes {status_bytes} "
            f"after {start + len(chunk)} of {len(data)} bytes (chunk_size={chunk_size})"
        )


def test_viewer_reconciles_history_total_with_status_bar_bytes_for_ping_pong_capture(tk_root):
    data = (FIXTURES_DIR / "ping-pong.bin").read_bytes()

    # Sizes down to 7 bytes, not 1. On this 29 kB capture a 1-byte sweep
    # means ~29,000 update_ops calls, each re-rendering a receipt that stays
    # open until the very last byte, and it cost around 9 seconds of the whole
    # suite on its own. The defect this guards against -- chunks that produce
    # no ops being dropped from the byte accounting -- still occurs in the
    # thousands at 7 bytes, so the coverage loss is negligible. The multi-cut
    # test below still sweeps down to 1 byte, where the stream is small enough
    # for that to be free.
    for chunk_size in (len(data), 4096, 1024, 512, 128, 7):
        viewer = Viewer(width_dots=384, master=tk_root)
        try:
            _drive_and_assert_reconciled(viewer, data, chunk_size)

            # The capture has exactly one CutOp, at the very end, so it
            # always yields a single, fully-closed receipt covering every
            # byte -- regardless of chunk size. (The *number* of ops that
            # single receipt holds can vary slightly at very small chunk
            # sizes: the parser flushes a text run at the end of whatever
            # chunk it arrived in, so a tiny chunk can split one text run
            # into two TextOps -- a documented parser behavior, not a
            # byte-accounting one, and not what this test is about.)
            receipts = viewer.get_receipts()
            assert len(receipts) == 1
            assert receipts[0].byte_count == len(data)
            if chunk_size >= 512:
                assert sum(len(r.ops) for r in receipts) == 88
        finally:
            viewer.destroy()


def test_viewer_same_input_produces_the_same_per_receipt_byte_counts_whole_or_chunked(tk_root):
    data = (FIXTURES_DIR / "ping-pong.bin").read_bytes()

    def byte_counts_for(chunk_size: int) -> list:
        viewer = Viewer(width_dots=384, master=tk_root)
        try:
            _drive_and_assert_reconciled(viewer, data, chunk_size)
            return [r.byte_count for r in viewer.get_receipts()]
        finally:
            viewer.destroy()

    whole = byte_counts_for(len(data))
    chunked_4096 = byte_counts_for(4096)
    chunked_1024 = byte_counts_for(1024)
    chunked_128 = byte_counts_for(128)

    assert whole == chunked_4096 == chunked_1024 == chunked_128 == [len(data)]


def test_viewer_reconciles_a_stream_with_multiple_cuts(tk_root):
    # Three receipts: two closed by a cut, one left genuinely open (never
    # cut), plus a command deliberately split across a chunk boundary
    # (the lone trailing 0x1D) so a chunk that completes no op at all is
    # exercised too.
    data = (
        b"FIRST\x0a"
        + bytes([0x1D, 0x56, 0x00])  # GS V 0 -- full cut
        + b"SECOND\x0a"
        + bytes([0x1D, 0x56, 0x01])  # GS V 1 -- partial cut
        + b"THIRD"
        + bytes([0x1D])  # incomplete cut command -- never finishes
    )

    for chunk_size in (1, 2, 3, 7, len(data)):
        viewer = Viewer(width_dots=384, master=tk_root)
        try:
            _drive_and_assert_reconciled(viewer, data, chunk_size)

            receipts = viewer.get_receipts()
            assert len(receipts) == 3
            assert sum(r.byte_count for r in receipts) == len(data)
        finally:
            viewer.destroy()


def test_viewer_scroll_to_receipt_moves_the_canvas_view(tk_root, monkeypatch):
    viewer = _make_viewer(tk_root)
    try:
        viewer.update_ops([TextOp(text="A", style=PrinterState()), CutOp(mode="full")])
        viewer.update_ops([TextOp(text="B", style=PrinterState())])

        moved_to = []
        monkeypatch.setattr(viewer._canvas, "yview_moveto", lambda f: moved_to.append(f))

        viewer.scroll_to_receipt(2)

        assert moved_to
    finally:
        viewer.destroy()


# -- diagnostics panel ----------------------------------------------------


def test_viewer_diagnostics_counter_starts_at_a_calm_zero_state(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        assert viewer.get_diagnostics_count() == 0
        assert "no unknown" in viewer._diagnostics_var.get().lower()
    finally:
        viewer.destroy()


def test_viewer_add_diagnostics_updates_the_counter_and_label(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.add_diagnostics(
            [Diagnostic(offset=0, raw_bytes=b"\x1b\xff", reason="unknown command", severity="warning")]
        )

        assert viewer.get_diagnostics_count() == 1
        assert "1" in viewer._diagnostics_var.get()
    finally:
        viewer.destroy()


def test_viewer_diagnostics_window_shows_source_attribution_when_present(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.add_diagnostics(
            [
                Diagnostic(
                    offset=0,
                    raw_bytes=b"\x1b\xff",
                    reason="unknown command",
                    severity="warning",
                    source="opened file: capture.bin",
                )
            ]
        )

        listbox = viewer._show_diagnostics_window()

        assert "opened file: capture.bin" in listbox.get(0)
    finally:
        viewer.destroy()


def test_viewer_diagnostics_window_omits_source_when_not_attributed(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.add_diagnostics(
            [Diagnostic(offset=0, raw_bytes=b"\x1b\xff", reason="unknown command", severity="warning")]
        )

        listbox = viewer._show_diagnostics_window()

        assert "source=" not in listbox.get(0)
    finally:
        viewer.destroy()


# -- 32-column ruler ------------------------------------------------------


def test_viewer_ruler_is_off_by_default(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        assert viewer.is_ruler_visible() is False
    finally:
        viewer.destroy()


def test_viewer_set_ruler_visible_toggles_state(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.set_ruler_visible(True)
        assert viewer.is_ruler_visible() is True
        viewer.set_ruler_visible(False)
        assert viewer.is_ruler_visible() is False
    finally:
        viewer.destroy()


# -- always on top ----------------------------------------------------------


def test_viewer_always_on_top_is_off_by_default(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        assert not bool(viewer._root.attributes("-topmost"))
    finally:
        viewer.destroy()


def test_viewer_set_always_on_top_toggles_the_window_attribute(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        viewer.set_always_on_top(True)
        assert bool(viewer._root.attributes("-topmost"))
        viewer.set_always_on_top(False)
        assert not bool(viewer._root.attributes("-topmost"))
    finally:
        viewer.destroy()


# -- Open button ------------------------------------------------------------


def test_viewer_open_click_invokes_on_open_callback_with_the_chosen_path(tk_root, monkeypatch):
    calls = []
    viewer = Viewer(width_dots=384, master=tk_root, on_open=lambda path: calls.append(path))
    try:
        monkeypatch.setattr("render.viewer.filedialog.askopenfilename", lambda **kw: "C:/tmp/capture.bin")

        viewer._on_open_click()

        assert calls == ["C:/tmp/capture.bin"]
    finally:
        viewer.destroy()


def test_viewer_open_click_does_nothing_when_dialog_is_cancelled(tk_root, monkeypatch):
    calls = []
    viewer = Viewer(width_dots=384, master=tk_root, on_open=lambda path: calls.append(path))
    try:
        monkeypatch.setattr("render.viewer.filedialog.askopenfilename", lambda **kw: "")

        viewer._on_open_click()

        assert calls == []
    finally:
        viewer.destroy()


# -- closed-receipt render caching (performance fix) ---------------------
#
# update_ops() used to re-render every receipt (including already-closed
# ones whose pixels can never change again) on every call, so a big
# capture arriving in N chunks paid for re-rasterising its embedded
# images ~N times. These tests pin the caching contract: a closed
# receipt is rendered exactly once, the still-open trailing receipt is
# always rendered fresh, and every entry point that replaces what is
# displayed drops the cache instead of serving stale bitmaps.


def _spy_on_renderer_render(monkeypatch):
    """Wrap `ReceiptRenderer.render` so each call's `ops` argument is
    recorded, while still delegating to the real implementation."""
    from render.raster import ReceiptRenderer

    calls = []
    original_render = ReceiptRenderer.render

    def spy(self, ops):
        calls.append(list(ops))
        return original_render(self, ops)

    monkeypatch.setattr(ReceiptRenderer, "render", spy)
    return calls


def test_viewer_closed_receipt_is_rendered_exactly_once_across_many_update_ops_calls(tk_root, monkeypatch):
    viewer = _make_viewer(tk_root)
    try:
        calls = _spy_on_renderer_render(monkeypatch)

        viewer.update_ops([TextOp(text="A", style=PrinterState()), CutOp(mode="full")])
        assert len(calls) == 1  # the newly-closed receipt is rendered once

        for i in range(10):
            # None of these calls add a new cut -- receipt #1 (index 0)
            # stays closed and must never be re-rendered again.
            viewer.update_ops([TextOp(text=f"B{i}", style=PrinterState())])

        closed_receipt_renders = [call for call in calls if call and isinstance(call[-1], CutOp)]
        assert len(closed_receipt_renders) == 1
    finally:
        viewer.destroy()


def test_viewer_open_receipt_rerenders_on_every_update_ops_call(tk_root, monkeypatch):
    viewer = _make_viewer(tk_root)
    try:
        calls = _spy_on_renderer_render(monkeypatch)

        viewer.update_ops([TextOp(text="A", style=PrinterState())])
        viewer.update_ops([TextOp(text="B", style=PrinterState())])
        viewer.update_ops([TextOp(text="C", style=PrinterState())])

        # No CutOp ever arrives: every call re-renders the same still-open
        # trailing receipt, because new ops can still extend it -- caching
        # it would freeze it mid-print.
        assert len(calls) == 3
    finally:
        viewer.destroy()


def test_viewer_clear_drops_the_receipt_cache_so_the_same_bytes_render_again(tk_root, monkeypatch):
    viewer = _make_viewer(tk_root)
    try:
        calls = _spy_on_renderer_render(monkeypatch)

        # clear() unconditionally resets _current_image via
        # self._renderer.render([]) -- a pre-existing, cheap "blank
        # canvas" call unrelated to receipt caching. Filter it out so
        # this test only counts renders of actual receipt content.
        def receipt_render_count() -> int:
            return len([call for call in calls if call])

        closing_ops = [TextOp(text="A", style=PrinterState()), CutOp(mode="full")]
        viewer.update_ops(list(closing_ops))
        assert receipt_render_count() == 1

        viewer.update_ops([TextOp(text="still open", style=PrinterState())])
        assert receipt_render_count() == 2  # closed receipt cached (no new render), open receipt rendered

        viewer.clear()
        viewer.update_ops(list(closing_ops))

        # Same closed-receipt bytes fed again after clear() must render
        # fresh: clear() must drop the whole cache, not just the ops list.
        assert receipt_render_count() == 3
    finally:
        viewer.destroy()


def test_viewer_open_after_clear_shows_no_stale_cached_receipt_images(tk_root, monkeypatch):
    # Mirrors main.py._make_open_handler()'s wholesale-replace path for
    # the Open button/--replay: clear() the viewer, then feed the newly
    # opened file's ops. No receipt image from the file that was showing
    # before may survive into the new content.
    viewer = _make_viewer(tk_root)
    try:
        calls = _spy_on_renderer_render(monkeypatch)

        viewer.update_ops([TextOp(text="FILE_A", style=PrinterState()), CutOp(mode="full")])
        assert len(calls) == 1

        viewer.clear()
        viewer.update_ops([TextOp(text="FILE_B", style=PrinterState()), CutOp(mode="full")])

        receipts = viewer.get_receipts()
        assert len(receipts) == 1
        assert receipts[0].ops[0].text == "FILE_B"

        # Ignore clear()'s own unconditional render([]) "blank canvas"
        # call (pre-existing, unrelated to receipt caching): only actual
        # receipt-content renders matter here.
        receipt_render_calls_after_clear = [call for call in calls[1:] if call]
        assert len(receipt_render_calls_after_clear) == 1
        assert receipt_render_calls_after_clear[0][0].text == "FILE_B"
    finally:
        viewer.destroy()


def test_viewer_changing_paper_width_invalidates_the_receipt_cache(tk_root, monkeypatch):
    # There is no public runtime "change paper width" API today (main.py
    # always constructs a fresh Viewer per --width), but the cache key
    # must still be defensively scoped to width_dots: a naive cache keyed
    # only on receipt identity/index would otherwise silently serve a
    # bitmap rendered for one width when asked to render the same
    # receipt at another width.
    viewer = _make_viewer(tk_root)
    try:
        calls = _spy_on_renderer_render(monkeypatch)

        viewer.update_ops([TextOp(text="A", style=PrinterState()), CutOp(mode="full")])
        assert len(calls) == 1
        receipt = viewer.get_receipts()[0]

        image_w384 = viewer._get_cached_receipt_image(0, receipt)
        assert len(calls) == 1  # still cached at width 384

        viewer.width_dots = 576
        image_w576 = viewer._get_cached_receipt_image(0, receipt)

        assert len(calls) == 2  # different width -> fresh render, not the width-384 bitmap
        assert image_w384 is not image_w576
    finally:
        viewer.destroy()


def test_viewer_same_input_produces_byte_identical_final_image_whole_or_chunked(tk_root):
    # Regression guard for the caching fix: the final rendered image must
    # be pixel-for-pixel identical to what it was before caching was
    # added, regardless of how the input was chunked.
    data = (FIXTURES_DIR / "ping-pong.bin").read_bytes()

    def final_image_bytes(chunk_size: int) -> bytes:
        viewer = Viewer(width_dots=384, master=tk_root)
        try:
            _drive_and_assert_reconciled(viewer, data, chunk_size)
            return viewer.get_current_image().tobytes()
        finally:
            viewer.destroy()

    whole = final_image_bytes(len(data))
    chunked_4096 = final_image_bytes(4096)
    chunked_1024 = final_image_bytes(1024)
    chunked_128 = final_image_bytes(128)

    assert whole == chunked_4096 == chunked_1024 == chunked_128


# -- default window geometry -------------------------------------------------


def test_viewer_requests_default_geometry_on_init(tk_root):
    viewer = _make_viewer(tk_root)
    try:
        tk_root.update_idletasks()
        size_part = tk_root.geometry().split("+")[0]
        assert size_part == DEFAULT_GEOMETRY
        assert DEFAULT_GEOMETRY == "900x700"
    finally:
        viewer.destroy()


def test_viewer_geometry_does_not_change_for_a_long_receipt(tk_root, monkeypatch):
    # Assert on what the viewer REQUESTS, not on what Tk reports back. The
    # shared root is withdrawn and other tests in this module create viewers
    # on it, so reading tk_root.geometry() depends on window mapping and on
    # test order -- which made this test fail intermittently. Same approach as
    # test_viewer_never_calls_minsize below.
    calls = []
    monkeypatch.setattr(tk_root, "geometry", lambda *a, **k: calls.append((a, k)))

    viewer = _make_viewer(tk_root)
    try:
        assert calls == [((DEFAULT_GEOMETRY,), {})]

        many_ops = []
        for i in range(500):
            many_ops.append(TextOp(text=f"line {i}", style=PrinterState()))
            many_ops.append(LineFeedOp())
        viewer.update_ops(many_ops)

        # A long receipt must scroll inside the window, never resize it.
        assert calls == [((DEFAULT_GEOMETRY,), {})]
    finally:
        viewer.destroy()


def test_viewer_never_calls_minsize(tk_root, monkeypatch):
    calls = []
    monkeypatch.setattr(tk_root, "minsize", lambda *a, **k: calls.append((a, k)))
    viewer = _make_viewer(tk_root)
    try:
        assert calls == []
    finally:
        viewer.destroy()


# -- get_status accessor ------------------------------------------------------


def test_get_status_returns_the_current_status(tk_root):
    status = _make_status().listening()
    viewer = Viewer(width_dots=384, master=tk_root, status=status)
    try:
        assert viewer.get_status() is status
    finally:
        viewer.destroy()


def test_get_status_reflects_update_status_calls(tk_root):
    viewer = Viewer(width_dots=384, master=tk_root, status=_make_status())
    try:
        new_status = _make_status().listening().add_bytes(10).add_ops(1)
        viewer.update_status(new_status)
        assert viewer.get_status() is new_status
    finally:
        viewer.destroy()
