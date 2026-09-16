"""Tkinter live window showing the rendered receipt.

Accumulates render ops as they arrive from the core parser and
re-renders the receipt area as a stack of separate receipt blocks (one
per `CutOp`-terminated group, see `render/receipts.py`). Only depends on
`core.ops`/`core.state` (the op vocabulary), `core.diagnostics`
(structured diagnostics vocabulary), `render.raster` and
`render.receipts`/`render.scroll_policy`; it never imports from
`transport/`.

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
"""

from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import filedialog
from typing import Callable, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageTk

from core.diagnostics import Diagnostic
from render.raster import ReceiptRenderer, ruler_x_position
from render.receipts import Receipt, split_into_receipts
from render.scroll_policy import is_scrolled_to_bottom, should_autoscroll_on_new_content

logger = logging.getLogger("btprinter.render.viewer")

_MAX_VISIBLE_HEIGHT = 2000  # viewport cap; the full image is reachable via the scrollbar
_EMPTY_STATE_HEIGHT = 160  # canvas height while showing the placeholder text
_SEPARATOR_HEIGHT = 14  # visible gap drawn between two receipt blocks
_SEPARATOR_FILL = 190  # light gray, so separate receipts read as separate sheets
_RULER_FONT = "A"  # the ruler always marks the Font A column budget for the active width

_CONNECTION_STATE_LABELS = {
    "starting": "starting...",
    "listening": "listening for a client",
    "connected": "client connected",
    "disconnected": "client disconnected, waiting for a new client",
    "stopped": "stopped",
}


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

    state_label = _CONNECTION_STATE_LABELS.get(status.connection_state, status.connection_state)
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
        self._current_image: Image.Image = self._renderer.render([])
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
        self._receipts = split_into_receipts(self._ops, byte_offsets=self._op_byte_offsets)

        image, offsets = self._build_combined_image()
        self._current_image = image
        self._receipt_offsets = offsets

        self._refresh_body()
        self._refresh_history_panel()

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
        self._current_image = self._renderer.render([])

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
        """
        if not self._receipts:
            return self._renderer.render([]), []

        images = [self._renderer.render(list(receipt.ops)) for receipt in self._receipts]
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
