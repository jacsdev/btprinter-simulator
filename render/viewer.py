"""Tkinter live window showing the rendered receipt.

Accumulates render ops as they arrive from the core parser and
re-renders the receipt area as a stack of separate receipt blocks (one
per `CutOp`-terminated group, see `render/receipts.py`). Only depends on
`core.ops`/`core.state` (the op vocabulary), `core.diagnostics`
(structured diagnostics vocabulary), `render.raster` and
`render.receipts`/`render.scroll_policy`; it never imports from
`transport/`.

A receipt already closed by a cut is immutable, so its rendered bitmap
is cached instead of being rebuilt on every `update_ops()` call (a
capture arriving in N chunks used to re-render every closed receipt's
embedded images ~N times); only the still-open trailing receipt is
always rendered fresh. See `_get_cached_receipt_image()`.

Also owns the persistent status bar and the empty-state placeholder
shown before any receipt has been rendered (`TransportStatus`,
`format_status_line`, `format_empty_state_message`). `main.py`, the
composition root, is the only place that knows about both the
transport adapters and the viewer -- it builds/updates `TransportStatus`
instances and hands them to `Viewer.update_status()`, and wires the
`on_clear`/`on_open` callbacks so the Clear/Open buttons can reach
composition-root concerns (resetting transport-owned counters, feeding
a replayed file through a fresh `Parser`) without `render/` importing
`core.parser` or `transport/` itself.

Keybinding: press "s" while the window has focus to save the current
receipt to a PNG file (prompted via a simple save dialog).

"Save capture..." (toolbar button) exports the *raw* ESC/POS byte
stream instead of a rendered image -- see `append_raw_bytes()` and
`get_capture_target()` below. The need for a raw capture is almost
always discovered only after something already looks wrong on screen,
by which point a capture that only started recording from a button
press would have missed everything; the viewer therefore always
buffers received bytes (bounded, see `_RAW_CAPTURE_MAX_BYTES`) so the
export is retroactive.
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass, replace
from tkinter import filedialog, messagebox
from typing import Callable, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageTk

from core.comparative_decode import decode_under_candidates
from core.diagnostics import Diagnostic
from core.ops import CutOp, TextOp
from render.raster import ReceiptRenderer, ruler_x_position
from render.receipts import Receipt, split_into_receipts
from render.scroll_policy import is_scrolled_to_bottom, should_autoscroll_on_new_content

logger = logging.getLogger("btprinter.render.viewer")

DEFAULT_GEOMETRY = "900x700"  # sensible default window size; never grows/shrinks with receipt content
_MAX_VISIBLE_HEIGHT = 2000  # viewport cap; the full image is reachable via the scrollbar
_EMPTY_STATE_HEIGHT = 160  # canvas height while showing the placeholder text
_SEPARATOR_HEIGHT = 14  # visible gap drawn between two receipt blocks
_SEPARATOR_FILL = 190  # light gray, so separate receipts read as separate sheets
_RULER_FONT = "A"  # the ruler always marks the Font A column budget for the active width

# Cap on how many closed-receipt bitmaps _get_cached_receipt_image() keeps
# around at once. Without a cap, a very long session (or a single huge
# replayed capture with many cuts) would accumulate one full-resolution
# PIL Image per closed receipt for the lifetime of the process. 64 is a
# generous multiple of what a person normally scrolls back through in one
# sitting; entries beyond the cap are evicted least-recently-used and
# simply re-rendered on demand if they are needed again (a cache miss,
# never a correctness issue -- see _get_cached_receipt_image()).
_RECEIPT_CACHE_MAX_ENTRIES = 64

# Cap on the in-memory raw byte buffer backing "Save capture..." (see
# append_raw_bytes()). A heavy real-world print is around 30 KB, so 8 MiB
# holds roughly 250 heavy prints' worth of raw bytes -- generous for
# anything a person would do in one sitting -- while still bounding
# memory for a simulator left running unattended for a long time
# (--dump-bytes remains the right tool for that case; it writes straight
# to disk instead of holding everything in RAM). Once the cap is
# exceeded, the OLDEST bytes are discarded -- the most recent activity
# is what a person actually wants to inspect after noticing something
# wrong -- and `_raw_capture_truncated` is recorded so the capture UI
# can say the result is partial instead of silently handing back a
# short file with no explanation. Trade-off: a session that runs long
# enough to exceed this cap can no longer export its earliest bytes
# from memory; --dump-bytes is unaffected and keeps the full history on
# disk regardless of this cap.
_RAW_CAPTURE_MAX_BYTES = 8 * 1024 * 1024

_CONNECTION_STATE_LABELS = {
    "starting": "starting...",
    "listening": "listening for a client",
    "connected": "client connected",
    "disconnected": "client disconnected, waiting for a new client",
    "stopped": "stopped",
    "data_flowing": "receiving data",
}

# The serial transport (transport/serialport.py) has no accept boundary it
# can observe: by the time it opens the COM port, Windows has already
# paired and connected the peer. Its "listening" state therefore means
# something different from tcp/rfcomm's -- "open, no bytes flowing right
# now" rather than "waiting for a client to connect" -- and must be worded
# accordingly instead of falsely claiming to watch for a connection
# handshake it cannot see.
_SERIAL_CONNECTION_STATE_LABELS = {
    "listening": "open, waiting for data",
}


def _connection_state_label(status: "TransportStatus") -> str:
    """Resolve the display label for `status.connection_state`, honoring
    the serial-specific override above where it applies."""
    if status.transport == "serial":
        serial_label = _SERIAL_CONNECTION_STATE_LABELS.get(status.connection_state)
        if serial_label is not None:
            return serial_label
    return _CONNECTION_STATE_LABELS.get(status.connection_state, status.connection_state)


@dataclass(frozen=True)
class TransportStatus:
    """Immutable snapshot of the simulator's transport state.

    Drives both the persistent status bar (`format_status_line`) and the
    empty-state placeholder (`format_empty_state_message`). Every
    transition method below returns a new instance rather than mutating
    in place, so a caller can safely hand a snapshot to the viewer while
    computing the next one.
    """

    transport: str  # "tcp" | "rfcomm" | "serial"
    endpoint: str  # e.g. "channel 4", "port 9100", "COM5"
    width_mm: int
    codepages_label: str
    sdp_uuid: Optional[str] = None
    connection_state: str = "starting"
    peer: Optional[str] = None
    bytes_received: int = 0
    ops_count: int = 0
    # Set only for --replay / the Open button's replayed file: when not
    # None, the status bar and empty-state message describe a replay
    # session instead of a live transport, and must never claim to be
    # listening for a client.
    replay_path: Optional[str] = None

    def listening(self) -> "TransportStatus":
        return replace(self, connection_state="listening")

    def client_connected(self, peer: Optional[str] = None) -> "TransportStatus":
        return replace(self, connection_state="connected", peer=peer)

    def client_disconnected(self) -> "TransportStatus":
        return replace(self, connection_state="disconnected", peer=None)

    def stopped(self) -> "TransportStatus":
        return replace(self, connection_state="stopped")

    def data_flowing(self) -> "TransportStatus":
        """The serial transport's honest stand-in for `client_connected()`:
        it cannot observe an accept handshake, only that bytes started
        arriving after a quiet period (see transport/serialport.py)."""
        return replace(self, connection_state="data_flowing")

    def with_endpoint(self, endpoint: str, sdp_uuid: Optional[str] = None) -> "TransportStatus":
        """Update the endpoint (and optionally the SDP UUID) once the
        transport has actually started and the real channel/port/COM
        name is known, without disturbing connection_state or counters.
        """
        if sdp_uuid is None:
            return replace(self, endpoint=endpoint)
        return replace(self, endpoint=endpoint, sdp_uuid=sdp_uuid)

    def add_bytes(self, count: int) -> "TransportStatus":
        return replace(self, bytes_received=self.bytes_received + count)

    def add_ops(self, count: int) -> "TransportStatus":
        return replace(self, ops_count=self.ops_count + count)

    def reset_counters(self) -> "TransportStatus":
        """Reset the byte/op counters shown in the status bar -- what
        the Clear ("new paper") button asks for. Never touches the
        transport connection state or the code page configuration.
        """
        return replace(self, bytes_received=0, ops_count=0)


def format_status_line(status: TransportStatus) -> str:
    """Render a TransportStatus as the single-line text shown in the
    persistent status bar."""
    if status.replay_path is not None:
        parts = [
            "Mode: REPLAY (no transport)",
            f"File: {status.replay_path}",
            f"Paper: {status.width_mm}mm",
            f"Code pages: {status.codepages_label}",
            f"Bytes received: {status.bytes_received}",
            f"Render ops: {status.ops_count}",
        ]
        return " | ".join(parts)

    parts = [
        f"Transport: {status.transport.upper()}",
        f"Endpoint: {status.endpoint}",
    ]
    if status.sdp_uuid:
        parts.append(f"SDP UUID: {status.sdp_uuid}")
    parts.append(f"Paper: {status.width_mm}mm")
    parts.append(f"Code pages: {status.codepages_label}")

    state_label = _connection_state_label(status)
    if status.connection_state == "connected" and status.peer:
        state_label = f"{state_label} ({status.peer})"
    parts.append(f"Status: {state_label}")

    parts.append(f"Bytes received: {status.bytes_received}")
    parts.append(f"Render ops: {status.ops_count}")
    return " | ".join(parts)


def format_empty_state_message(status: Optional[TransportStatus]) -> str:
    """Placeholder text shown in the receipt area before any ESC/POS
    bytes have been received. Must make it obvious the simulator is
    alive and idle -- never a blank canvas that could be mistaken for a
    frozen window."""
    if status is None:
        return (
            "Simulator is running and idle.\n"
            "Waiting for a client to connect.\n"
            "No receipt data received yet."
        )
    if status.replay_path is not None:
        return (
            f"Replay mode: showing {status.replay_path}.\n"
            "No transport is running -- this is not a live connection."
        )
    if status.connection_state == "connected":
        peer_note = f" ({status.peer})" if status.peer else ""
        return (
            f"Client connected via {status.transport.upper()}{peer_note}.\n"
            "Waiting for the first ESC/POS bytes..."
        )
    if status.connection_state == "data_flowing":
        # The serial transport's honest stand-in for "connected" -- it has
        # no accept boundary to report a peer address for.
        return (
            f"Receiving data via {status.transport.upper()}.\n"
            "No receipt rendered yet from the bytes received so far."
        )
    if status.transport == "serial":
        # Serial has no accept boundary to observe -- never claim to be
        # waiting for a "client to connect" the way tcp/rfcomm can.
        return (
            "Simulator is running and idle.\n"
            f"Waiting for data on {status.endpoint}.\n"
            "No receipt data received yet."
        )
    return (
        "Simulator is running and idle.\n"
        f"Waiting for a client to connect via {status.transport.upper()} on {status.endpoint}.\n"
        "No receipt data received yet."
    )


def _format_diagnostics_label(count: int) -> str:
    """A calm, neutral label for zero diagnostics -- never an alarming
    empty panel -- and a plain count otherwise."""
    if count == 0:
        return "No unknown commands"
    return f"{count} unknown command{'s' if count != 1 else ''}"


@dataclass(frozen=True)
class CaptureTarget:
    """What "Save capture..." is about to write.

    Built by `Viewer.get_capture_target()` from whatever is currently
    selected in the session history panel -- a single receipt's bytes
    if one is selected, the whole session's buffered bytes otherwise.
    Deliberately Tk-free (it only reads the listbox's own current
    selection, never opens a dialog) so the selection logic is directly
    testable without mocking anything beyond the save dialog itself.
    """

    label: str  # human-readable description shown in the save dialog title
    data: bytes  # the raw bytes that would be written
    is_partial: bool  # True if the capture buffer's cap already dropped some of this range
    default_filename: str


class Viewer:
    """Live receipt window plus a headless-testable rendering pipeline.

    `get_current_image()`, `update_ops()`, `clear()` and `save_png()`
    never touch the Tkinter mainloop, so they can be exercised in tests
    without blocking. `run()` enters the blocking mainloop for
    interactive use.
    """

    def __init__(
        self,
        width_dots: int = 384,
        title: str = "58mm Receipt Simulator",
        master: Optional[tk.Misc] = None,
        status: Optional[TransportStatus] = None,
        on_clear: Optional[Callable[[], None]] = None,
        on_open: Optional[Callable[[str], None]] = None,
    ) -> None:
        """`master`: an existing Tk widget to host the canvas in, instead of
        creating a new `Tk()` interpreter. Mainly for tests, which share one
        root across several Viewer instances to avoid repeatedly
        creating/tearing down a Tcl interpreter in the same process.

        `status`: the initial `TransportStatus` to show in the status bar
        and empty-state message. `main.py` passes one built from CLI args;
        it is optional so existing callers/tests that only care about the
        rendering pipeline keep working unchanged.

        `on_clear`/`on_open`: composition-root callbacks for the Clear and
        Open buttons. `render/` must not import `core.parser` or
        `transport/`, so any behaviour beyond "reset this widget's own
        state" (resetting transport-owned counters, parsing a replayed
        file) is delegated back to whoever constructed the Viewer. Both
        can also be set later via `set_clear_handler()`/`set_open_handler()`.
        """
        self.width_dots = width_dots
        self._renderer = ReceiptRenderer(width_dots=width_dots)
        self._ops: List[object] = []
        self._receipts: List[Receipt] = []
        self._receipt_offsets: List[int] = []
        self._op_byte_offsets: List[int] = []
        self._bytes_total = 0
        self._diagnostics: List[Diagnostic] = []
        self._ruler_visible = False
        # Cache of already-rendered CLOSED receipts, keyed by (receipt
        # index, width_dots) -- see _get_cached_receipt_image(). The still
        # -open trailing receipt is never stored here: new ops can still
        # extend it, so it must always be rendered fresh. An OrderedDict
        # gives cheap LRU eviction via move_to_end()/popitem(last=False).
        self._receipt_image_cache: "OrderedDict[Tuple[int, int], Image.Image]" = OrderedDict()
        self._current_image: Image.Image = self._renderer.render([])
        # Raw byte capture buffer backing "Save capture..." -- see
        # append_raw_bytes()/_RAW_CAPTURE_MAX_BYTES. Independent of
        # self._ops/self._receipts: it holds the bytes themselves, not
        # what the parser made of them, so a capture can be exported
        # byte-for-byte regardless of how it renders.
        self._raw_capture: bytearray = bytearray()
        # Global offset of self._raw_capture[0] -- i.e. how many bytes
        # have been evicted from the front of the buffer so far. 0 until
        # the cap is first exceeded.
        self._raw_capture_start_offset: int = 0
        # Total bytes ever handed to append_raw_bytes(), independent of
        # how many are still actually held (some may have been evicted).
        self._raw_capture_total_seen: int = 0
        self._raw_capture_truncated: bool = False
        self._status: Optional[TransportStatus] = status
        self._on_clear = on_clear
        self._on_open = on_open

        self._owns_root = master is None
        if master is None:
            self._root: tk.Misc = tk.Tk()
            self._root.withdraw()  # stay hidden until show() / run() is called
            self._root.title(title)
        else:
            self._root = master

        # Sensible default window size, independent of receipt content --
        # the receipt canvas scrolls for long receipts instead of the
        # window growing to fit them (see _build_content_area()). Never
        # paired with minsize(): the user must still be able to shrink
        # the window below this size.
        self._root.geometry(DEFAULT_GEOMETRY)

        self._build_toolbar()
        self._build_status_bar()
        self._build_content_area()

        self._root.bind("<KeyPress-s>", self._on_save_key)

        self._refresh_status_bar()
        self._refresh_diagnostics_label()
        self._refresh_history_panel()
        self._refresh_body()

    # -- widget construction ---------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = tk.Frame(self._root)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self._clear_button = tk.Button(toolbar, text="Clear", command=self._on_clear_click)
        self._clear_button.pack(side=tk.LEFT, padx=2, pady=2)

        self._open_button = tk.Button(toolbar, text="Open...", command=self._on_open_click)
        self._open_button.pack(side=tk.LEFT, padx=2, pady=2)

        self._save_capture_button = tk.Button(
            toolbar, text="Save capture...", command=self._on_save_capture_click
        )
        self._save_capture_button.pack(side=tk.LEFT, padx=2, pady=2)

        self._ruler_var = tk.BooleanVar(master=self._root, value=False)
        self._ruler_check = tk.Checkbutton(
            toolbar, text="32-column ruler", variable=self._ruler_var, command=self._on_ruler_toggle
        )
        self._ruler_check.pack(side=tk.LEFT, padx=6)

        self._always_on_top_var = tk.BooleanVar(master=self._root, value=False)
        self._always_on_top_check = tk.Checkbutton(
            toolbar,
            text="Always on top",
            variable=self._always_on_top_var,
            command=self._on_always_on_top_toggle,
        )
        self._always_on_top_check.pack(side=tk.LEFT, padx=6)

        self._diagnostics_var = tk.StringVar(master=self._root, value=_format_diagnostics_label(0))
        self._diagnostics_label = tk.Label(toolbar, textvariable=self._diagnostics_var)
        self._diagnostics_label.pack(side=tk.LEFT, padx=6)

        self._diagnostics_button = tk.Button(toolbar, text="Details", command=self._show_diagnostics_window)
        self._diagnostics_button.pack(side=tk.LEFT, padx=2)

        self._codepage_compare_button = tk.Button(
            toolbar, text="Compare code pages...", command=self._show_codepage_comparison_window
        )
        self._codepage_compare_button.pack(side=tk.LEFT, padx=6)

    def _build_status_bar(self) -> None:
        self._status_var = tk.StringVar(master=self._root)
        self._status_label = tk.Label(
            self._root,
            textvariable=self._status_var,
            anchor="w",
            relief=tk.SUNKEN,
            bg="#f0f0f0",
        )
        self._status_label.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_content_area(self) -> None:
        content = tk.Frame(self._root)
        content.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        canvas_frame = tk.Frame(content)
        canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._v_scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self._canvas = tk.Canvas(
            canvas_frame,
            width=self.width_dots,
            height=1,
            bg="white",
            yscrollcommand=self._v_scrollbar.set,
        )
        self._v_scrollbar.config(command=self._canvas.yview)
        self._v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Mouse-wheel support: <MouseWheel> on Windows/macOS (event.delta
        # in multiples of 120), <Button-4>/<Button-5> on X11.
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._canvas.bind("<Button-4>", self._on_mousewheel)
        self._canvas.bind("<Button-5>", self._on_mousewheel)

        self._photo: Optional[ImageTk.PhotoImage] = None

        history_frame = tk.Frame(content, width=220)
        history_frame.pack(side=tk.RIGHT, fill=tk.Y)
        history_frame.pack_propagate(False)

        tk.Label(history_frame, text="Session history", anchor="w").pack(side=tk.TOP, fill=tk.X)

        history_list_frame = tk.Frame(history_frame)
        history_list_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        history_scrollbar = tk.Scrollbar(history_list_frame, orient=tk.VERTICAL)
        self._history_listbox = tk.Listbox(
            history_list_frame, yscrollcommand=history_scrollbar.set, exportselection=False
        )
        history_scrollbar.config(command=self._history_listbox.yview)
        history_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._history_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._history_listbox.bind("<<ListboxSelect>>", self._on_history_select)

    # -- headless-testable API -------------------------------------------

    def update_ops(
        self,
        ops: List[object],
        chunk_bytes: int = 0,
        byte_offsets: Optional[List[int]] = None,
    ) -> None:
        """Append newly parsed ops and re-render the receipt area.

        `chunk_bytes`, when known (the composition root passes the raw
        byte-chunk length that produced `ops`), is added to the running
        byte total shown in the status bar / used as a fallback below.

        `byte_offsets`, when supplied, must be the same length as `ops`:
        the exact per-op offsets *within this chunk* (e.g. from
        `core.parser.Parser.take_op_offsets()`), used to compute accurate
        per-receipt byte counts for the session history panel. This
        matters whenever a single chunk can contain more than one
        receipt -- replaying a whole captured file is exactly that case
        -- because without it, every op in the chunk would be attributed
        the same cumulative byte total, silently misattributing all of
        the chunk's bytes to whichever receipt happens to close last
        (previously observed as a receipt with real content reporting
        "0 bytes").

        When `byte_offsets` is omitted, this falls back to the previous
        coarse approximation (every op in the chunk reported at the
        chunk's cumulative end offset), which is only accurate when the
        chunk never spans more than one receipt. It defaults to 0/`None`
        so existing/simple callers that only care about the rendering
        pipeline are unaffected.

        A chunk that produces no ops at all (`ops == []`) -- e.g. the
        parser is still buffering a partial op, such as a raster image
        band split across more than one network read -- must still be
        accounted for: `chunk_bytes` is always added to the running byte
        total even when `ops` is empty, and that running total is passed
        to `split_into_receipts()` as `total_bytes` so the trailing,
        not-yet-cut receipt in the session history panel is topped up to
        match it. A caller that only invoked `update_ops()` when `ops`
        was non-empty would silently drop that chunk's bytes from the
        Viewer's own accounting while any separately maintained
        status-bar counter kept counting them -- two numbers that could
        never be reconciled again. Always call this once per chunk,
        empty or not.
        """
        was_at_bottom = is_scrolled_to_bottom(self._canvas.yview())

        self._ops.extend(ops)
        if byte_offsets is not None:
            if len(byte_offsets) != len(ops):
                raise ValueError("byte_offsets must be the same length as ops")
            base = self._bytes_total
            self._op_byte_offsets.extend(base + offset for offset in byte_offsets)
            self._bytes_total += chunk_bytes
        else:
            self._bytes_total += chunk_bytes
            self._op_byte_offsets.extend([self._bytes_total] * len(ops))
        self._receipts = split_into_receipts(
            self._ops, byte_offsets=self._op_byte_offsets, total_bytes=self._bytes_total
        )
        self._refresh_history_panel()

        if not ops:
            # Byte accounting and the history panel's trailing-receipt
            # label are already up to date; there is nothing new to
            # render, so skip rebuilding the (unchanged) receipt image.
            return

        image, offsets = self._build_combined_image()
        self._current_image = image
        self._receipt_offsets = offsets

        self._refresh_body()

        if should_autoscroll_on_new_content(was_at_bottom):
            self._canvas.yview_moveto(1.0)

    def clear(self) -> None:
        """"New paper": empty the receipt area, reset the session
        history and the byte/op counters -- but never disconnect the
        transport or reset the code page configuration. Those live in
        `main.py`'s composition-root state, which is why the transport-
        owned reset happens through the injected `on_clear` callback
        rather than here.
        """
        self._ops = []
        self._receipts = []
        self._receipt_offsets = []
        self._op_byte_offsets = []
        self._bytes_total = 0
        self._diagnostics = []
        # Wholesale content replacement: nothing cached under the old
        # receipt indices is valid for whatever comes next (a fresh
        # receipt #1 after clear() shares index 0 with the old receipt
        # #1, but is not the same receipt), so drop every cached bitmap
        # rather than let a stale one survive under a reused index.
        self._receipt_image_cache.clear()
        self._current_image = self._renderer.render([])
        # Same wholesale-replacement reasoning as the receipt cache above:
        # bytes captured before a Clear ("new paper") or an Open/--replay
        # reload belong to a session that no longer exists on screen, so
        # "Save capture..." must never mix them into what comes next.
        self._raw_capture = bytearray()
        self._raw_capture_start_offset = 0
        self._raw_capture_total_seen = 0
        self._raw_capture_truncated = False

        self._refresh_diagnostics_label()
        self._refresh_history_panel()
        self._refresh_body()
        self._canvas.yview_moveto(0.0)

        if self._on_clear is not None:
            self._on_clear()

    def add_diagnostics(self, diagnostics: List[Diagnostic]) -> None:
        """Record diagnostics produced by the parser (see
        `core.diagnostics.Diagnostic` / `Parser.take_diagnostics()`) and
        refresh the counter label."""
        if not diagnostics:
            return
        self._diagnostics.extend(diagnostics)
        self._refresh_diagnostics_label()

    def get_diagnostics(self) -> List[Diagnostic]:
        return list(self._diagnostics)

    def get_diagnostics_count(self) -> int:
        return len(self._diagnostics)

    def get_receipts(self) -> List[Receipt]:
        return list(self._receipts)

    def scroll_to_receipt(self, sequence: int) -> None:
        """Scroll the receipt area so the receipt with this sequence
        number is visible. Used by the session history panel."""
        for index, receipt in enumerate(self._receipts):
            if receipt.sequence == sequence:
                if self._current_image.height > 0 and index < len(self._receipt_offsets):
                    fraction = self._receipt_offsets[index] / self._current_image.height
                    self._canvas.yview_moveto(max(0.0, min(fraction, 1.0)))
                return

    def is_ruler_visible(self) -> bool:
        return self._ruler_visible

    def set_ruler_visible(self, value: bool) -> None:
        self._ruler_visible = bool(value)
        self._ruler_var.set(self._ruler_visible)
        self._refresh_body()

    def set_always_on_top(self, value: bool) -> None:
        value = bool(value)
        self._always_on_top_var.set(value)
        self._root.attributes("-topmost", value)

    def set_clear_handler(self, handler: Optional[Callable[[], None]]) -> None:
        self._on_clear = handler

    def set_open_handler(self, handler: Optional[Callable[[str], None]]) -> None:
        self._on_open = handler

    def get_status(self) -> Optional[TransportStatus]:
        """Return the current `TransportStatus` snapshot, or `None` if the
        viewer was constructed without one. Lets a composition-root
        callback (e.g. `main._make_open_handler()`'s `on_open`) read the
        currently displayed counters before accumulating on top of them
        via `update_status()`."""
        return self._status

    def update_status(self, status: TransportStatus) -> None:
        """Refresh the status bar (and the empty-state message, if it is
        still showing) from a new `TransportStatus` snapshot. Always
        called from the Tk thread -- callers on a background thread must
        go through `call_soon()`."""
        self._status = status
        self._refresh_status_bar()
        if not self._ops:
            self._refresh_body()

    def set_title(self, title: str) -> None:
        """Update the window title. Always called from the Tk thread --
        callers on a background thread must go through `call_soon()`."""
        self._root.title(title)

    def get_current_image(self) -> Image.Image:
        return self._current_image

    def call_soon(self, callback, *args) -> None:
        """Schedule `callback(*args)` to run on the Tk thread.

        Tkinter widgets are not thread-safe: transport adapters receive
        bytes on a background thread, so `main.py` must hop back onto the
        UI thread via this method before calling `update_ops`, `update_status`
        or `set_title`.
        """
        self._root.after(0, callback, *args)

    def save_png(self, path: str) -> None:
        self._current_image.convert("RGB").save(path, format="PNG")
        logger.info("saved receipt to %s", path)

    def append_raw_bytes(self, data: bytes) -> None:
        """Append newly received/loaded raw bytes to the in-memory
        capture buffer backing "Save capture..." (see
        `get_capture_target()`).

        Called once per chunk, in lockstep with `update_ops()` (same
        chunk, same call site -- see `main.py`'s `on_data()`,
        `_make_open_handler()` and `run_replay()`), so the buffer's own
        running total (`_raw_capture_total_seen`) always matches the
        byte-offset coordinate space `update_ops()` derives receipts'
        `byte_count` in. That is what lets `get_capture_target()` slice
        the buffer by receipt without needing render.receipts to know
        anything about raw bytes at all.

        Bounded by `_RAW_CAPTURE_MAX_BYTES`: once exceeded, the oldest
        bytes are dropped and `_raw_capture_truncated` is set so the
        capture UI can say the result is partial (see that constant's
        docstring for the trade-off). Independent of `--dump-bytes`,
        which writes straight to disk and is not subject to this cap.
        """
        if not data:
            return
        self._raw_capture.extend(data)
        self._raw_capture_total_seen += len(data)
        overflow = len(self._raw_capture) - _RAW_CAPTURE_MAX_BYTES
        if overflow > 0:
            del self._raw_capture[:overflow]
            self._raw_capture_start_offset += overflow
            self._raw_capture_truncated = True

    def _receipt_byte_range(self, index: int) -> Tuple[int, int]:
        """Global `[start, end)` byte offset -- in the same coordinate
        space as `append_raw_bytes()`'s running total -- spanned by the
        receipt at this position in `self._receipts`.

        Receipts partition `self._ops` (and therefore the byte stream)
        contiguously and in arrival order (see
        `render.receipts.split_into_receipts()`), so a receipt's start
        offset is just the sum of every earlier receipt's `byte_count`;
        no separate bookkeeping is needed.
        """
        start = sum(r.byte_count or 0 for r in self._receipts[:index])
        end = start + (self._receipts[index].byte_count or 0)
        return start, end

    def _sliced_capture(self, start: int, end: int) -> Tuple[bytes, bool]:
        """Return the raw bytes in `[start, end)` still held in the
        capture buffer, plus whether any bytes in that range have
        already been evicted by the buffer's cap (a partial capture).

        The buffer only ever evicts from the front, so a requested range
        can only be partial at its start, never in the middle or at the
        end.
        """
        buffer_start = self._raw_capture_start_offset
        buffer_end = buffer_start + len(self._raw_capture)
        clipped_start = max(start, buffer_start)
        clipped_end = min(end, buffer_end)
        is_partial = clipped_start > start
        if clipped_end <= clipped_start:
            return b"", is_partial
        rel_start = clipped_start - buffer_start
        rel_end = clipped_end - buffer_start
        return bytes(self._raw_capture[rel_start:rel_end]), is_partial

    def get_capture_target(self) -> CaptureTarget:
        """Determine what "Save capture..." is about to write.

        If a receipt is currently selected in the session history
        panel, only that receipt's raw bytes are targeted -- a single
        receipt is normally the fixture a person actually wants,
        whereas a whole session mixing several prints together is not.
        With no selection, the whole session's buffered bytes are
        targeted instead.

        Deliberately free of any dialog: this only reads the history
        listbox's own current selection, so it is directly testable
        (see tests/test_save_capture.py) without mocking `filedialog`.
        """
        selection = self._history_listbox.curselection()
        if selection and selection[0] < len(self._receipts):
            index = selection[0]
            receipt = self._receipts[index]
            start, end = self._receipt_byte_range(index)
            data, is_partial = self._sliced_capture(start, end)
            return CaptureTarget(
                label=f"receipt #{receipt.sequence} ({len(data)} bytes)",
                data=data,
                is_partial=is_partial,
                default_filename=f"receipt-{receipt.sequence}.bin",
            )

        data, is_partial = self._sliced_capture(0, self._raw_capture_total_seen)
        return CaptureTarget(
            label=f"whole session ({len(data)} bytes)",
            data=data,
            is_partial=is_partial,
            default_filename="session-capture.bin",
        )

    def save_capture(self, path: str, data: bytes) -> None:
        """Write raw captured bytes to `path`, byte-for-byte -- binary
        mode, no newline translation -- so the result is directly usable
        with `--replay`."""
        with open(path, "wb") as f:
            f.write(data)
        logger.info("saved capture to %s (%d bytes)", path, len(data))

    def destroy(self) -> None:
        try:
            self._canvas.destroy()
        except tk.TclError:
            pass
        if self._owns_root:
            try:
                self._root.destroy()
            except tk.TclError:
                pass

    # -- interactive API ---------------------------------------------------

    def show(self) -> None:
        self._root.deiconify()

    def run(self) -> None:
        """Enter the blocking Tkinter mainloop. Not exercised by tests."""
        self.show()
        self._root.mainloop()

    # -- internals -----------------------------------------------------

    def _build_combined_image(self) -> Tuple[Image.Image, List[int]]:
        """Render each receipt separately and stack them with a visible
        gap in between, so several prints read as several receipts
        instead of one endless strip. Returns the combined image and the
        y-pixel offset each receipt starts at (for `scroll_to_receipt`).

        A receipt already closed by a `GS V` cut can never change again,
        so its bitmap is rendered once and cached (see
        `_get_cached_receipt_image()`); only the still-open trailing
        receipt, if any, is rendered fresh on every call, since new ops
        can still arrive for it.
        """
        if not self._receipts:
            return self._renderer.render([]), []

        last_index = len(self._receipts) - 1
        images = [
            self._renderer.render(list(receipt.ops))
            if index == last_index and not self._is_closed_receipt(receipt)
            else self._get_cached_receipt_image(index, receipt)
            for index, receipt in enumerate(self._receipts)
        ]
        total_height = sum(img.height for img in images) + _SEPARATOR_HEIGHT * (len(images) - 1)
        combined = Image.new("L", (self.width_dots, max(total_height, 1)), color=255)

        offsets: List[int] = []
        y = 0
        draw: Optional[ImageDraw.ImageDraw] = None
        for index, img in enumerate(images):
            offsets.append(y)
            combined.paste(img, (0, y))
            y += img.height
            if index < len(images) - 1:
                if draw is None:
                    draw = ImageDraw.Draw(combined)
                draw.rectangle([0, y, self.width_dots, y + _SEPARATOR_HEIGHT - 1], fill=_SEPARATOR_FILL)
                y += _SEPARATOR_HEIGHT

        return combined, offsets

    @staticmethod
    def _is_closed_receipt(receipt: Receipt) -> bool:
        """A receipt is closed once it has been terminated by a `GS V`
        cut -- i.e. its last op is a `CutOp` (see `render.receipts.
        split_into_receipts()`). A closed receipt's ops never change
        again, so its rendered bitmap is safe to cache forever (subject
        to the LRU cap)."""
        return bool(receipt.ops) and isinstance(receipt.ops[-1], CutOp)

    def _get_cached_receipt_image(self, index: int, receipt: Receipt) -> Image.Image:
        """Render (and cache) a CLOSED receipt's image, keyed by its
        position in `self._receipts` plus `width_dots`.

        Keying on index rather than on the `Receipt` object's identity is
        safe here specifically because a closed receipt's ops -- and
        therefore the index it occupies -- never change again once
        closed: `update_ops()` only ever appends new ops after it.
        `width_dots` is folded into the key defensively, in case a future
        change makes it mutable after construction; today it never
        changes for the lifetime of a `Viewer`. Any wholesale content
        replacement (`clear()`, and therefore the Open button / --replay
        path that calls it) drops the whole cache instead, so a reused
        index can never collide with a different receipt's stale bitmap.
        """
        key = (index, self.width_dots)
        cached = self._receipt_image_cache.get(key)
        if cached is not None:
            self._receipt_image_cache.move_to_end(key)
            return cached

        image = self._renderer.render(list(receipt.ops))
        self._receipt_image_cache[key] = image
        self._receipt_image_cache.move_to_end(key)
        if len(self._receipt_image_cache) > _RECEIPT_CACHE_MAX_ENTRIES:
            self._receipt_image_cache.popitem(last=False)
        return image

    def _refresh_body(self) -> None:
        """Show the empty-state placeholder while no ops have arrived
        yet, or the rendered receipt stack once they have."""
        if self._ops:
            self._render_and_display(self._current_image)
        else:
            self._show_empty_state()

    def _refresh_status_bar(self) -> None:
        text = format_status_line(self._status) if self._status is not None else "Waiting for transport to start..."
        self._status_var.set(text)

    def _refresh_diagnostics_label(self) -> None:
        self._diagnostics_var.set(_format_diagnostics_label(len(self._diagnostics)))

    def _refresh_history_panel(self) -> None:
        self._history_listbox.delete(0, tk.END)
        for receipt in self._receipts:
            timestamp_label = receipt.timestamp.strftime("%H:%M:%S")
            bytes_label = f"{receipt.byte_count} bytes" if receipt.byte_count is not None else "? bytes"
            self._history_listbox.insert(tk.END, f"#{receipt.sequence}  {timestamp_label}  {bytes_label}")

    def _show_empty_state(self) -> None:
        message = format_empty_state_message(self._status)
        self._canvas.config(height=_EMPTY_STATE_HEIGHT, width=self.width_dots)
        self._photo = None
        self._canvas.delete("all")
        self._canvas.create_text(
            self.width_dots // 2,
            _EMPTY_STATE_HEIGHT // 2,
            text=message,
            fill="#555555",
            width=max(self.width_dots - 20, 1),
            justify=tk.CENTER,
        )
        self._canvas.config(scrollregion=(0, 0, self.width_dots, _EMPTY_STATE_HEIGHT))

    def _render_and_display(self, image: Image.Image) -> None:
        display_height = min(max(image.height, 1), _MAX_VISIBLE_HEIGHT)
        self._canvas.config(height=display_height, width=self.width_dots)
        self._photo = ImageTk.PhotoImage(image.convert("RGB"))
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self._canvas.config(scrollregion=(0, 0, self.width_dots, image.height))
        if self._ruler_visible:
            self._draw_ruler(image.height)

    def _draw_ruler(self, height: int) -> None:
        x = ruler_x_position(self.width_dots, _RULER_FONT)
        self._canvas.create_line(x, 0, x, max(height, 1), fill="red", dash=(4, 2), tags="ruler")

    def _on_save_key(self, event: tk.Event) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
            title="Save receipt as PNG",
        )
        if path:
            self.save_png(path)

    def _on_save_capture_click(self) -> None:
        """"Save capture...": export the raw byte capture buffer.

        Which slice is offered (selected receipt vs. whole session) is
        entirely decided by `get_capture_target()`; this method only
        adds the two genuinely Tk-dependent steps around it -- warning
        honestly when the buffer's cap already dropped part of what was
        requested, and the save dialog itself -- so those are the only
        parts a test would need to mock (see tests/test_save_capture.py).
        """
        target = self.get_capture_target()

        if target.is_partial:
            proceed = messagebox.askyesno(
                "Partial capture",
                (
                    "The in-memory capture buffer is capped at "
                    f"{_RAW_CAPTURE_MAX_BYTES} bytes, and the oldest bytes of "
                    f"this {target.label} have already been discarded.\n\n"
                    "Save the remaining bytes anyway?"
                ),
            )
            if not proceed:
                return

        path = filedialog.asksaveasfilename(
            defaultextension=".bin",
            filetypes=[("Captured ESC/POS bytes", "*.bin"), ("All files", "*.*")],
            initialfile=target.default_filename,
            title=f"Save capture: {target.label}",
        )
        if not path:
            return
        self.save_capture(path, target.data)

    def _on_clear_click(self) -> None:
        self.clear()

    def _on_open_click(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Captured ESC/POS bytes", "*.bin"), ("All files", "*.*")],
            title="Open a captured byte file",
        )
        if not path:
            return
        if self._on_open is not None:
            self._on_open(path)

    def _on_ruler_toggle(self) -> None:
        self.set_ruler_visible(self._ruler_var.get())

    def _on_always_on_top_toggle(self) -> None:
        self.set_always_on_top(self._always_on_top_var.get())

    def _on_mousewheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta, "units")

    def _on_history_select(self, event: tk.Event) -> None:
        selection = self._history_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        if index < len(self._receipts):
            self.scroll_to_receipt(self._receipts[index].sequence)

    def _show_diagnostics_window(self) -> tk.Listbox:
        window = tk.Toplevel(self._root)
        window.title("Diagnostics")
        listbox = tk.Listbox(window, width=80)
        listbox.pack(fill=tk.BOTH, expand=True)
        if not self._diagnostics:
            listbox.insert(tk.END, "No unknown commands in this session.")
        for diag in self._diagnostics:
            line = f"offset={diag.offset}  bytes={diag.raw_bytes.hex(' ')}  reason={diag.reason}"
            if diag.source:
                # Attributes a diagnostic to a specific opened file rather
                # than leaving it to blend into the live transport's count
                # (see core.diagnostics.Diagnostic.source).
                line += f"  source={diag.source}"
            listbox.insert(tk.END, line)
        return listbox

    def _show_codepage_comparison_window(self) -> tk.Listbox:
        """Open the comparative code page decoder (see
        `core.comparative_decode`): every text run accumulated so far,
        decoded under every candidate code page side by side.

        Pure inspection -- reads `self._ops` and `core.comparative_decode`
        only, never writes to `self._ops`, `self._current_image`, or any
        parser/render state, so opening this window can never change
        what the receipt itself renders as (see
        tests/test_viewer.py::test_viewer_opening_codepage_comparison_does_not_change_the_rendered_receipt).
        """
        window = tk.Toplevel(self._root)
        window.title("Compare code pages")
        listbox = tk.Listbox(window, width=100)
        listbox.pack(fill=tk.BOTH, expand=True)

        text_runs = [op for op in self._ops if isinstance(op, TextOp) and op.raw]
        if not text_runs:
            listbox.insert(tk.END, "No text runs to compare yet.")
            return listbox

        for index, op in enumerate(text_runs):
            listbox.insert(tk.END, f"-- run {index}: raw bytes {op.raw.hex(' ')} " f"(rendered as: {op.text!r}) --")
            candidates = decode_under_candidates(op.raw)
            for cid in sorted(candidates):
                listbox.insert(tk.END, f"    id={cid}: {candidates[cid]!r}")
        return listbox
