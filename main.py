"""Composition root: picks a transport adapter, wires it to the core
parser, and displays the result in the live Tkinter viewer.

    python main.py --transport tcp --port 9100 --width 58

Only the composition root is allowed to import from `transport/`,
`core/` and `render/` all at once — that is precisely what a
composition root is for. None of those three packages import each
other in the direction that would break the transport -> core -> render
boundary described in README.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
import threading
from typing import Callable, List, NamedTuple, Optional, Set

from core.commands import CODEPAGE_MAP
from core.diagnostics import Diagnostic
from core.parser import Parser
from render.viewer import TransportStatus, Viewer
from transport.port import ConnectionEvent, ConnectionListener, Port
from transport.rfcomm import RfcommPort
from transport.serialport import SerialPort
from transport.tcp import TcpPort

logger = logging.getLogger("btprinter.main")

# 203 dpi paper widths in dots. Configurable, not hardcoded per receipt.
_WIDTH_DOTS_BY_MM = {58: 384, 80: 576}

# Well-known Bluetooth SIG Serial Port Profile (SPP) UUID -- the same one
# transport.rfcomm.make_spp_guid() encodes into the SDP record via
# WSASetServiceW. Duplicated here as a display-only string for the status
# bar: main.py only shows it, it never re-derives or re-registers it.
_SPP_SERVICE_UUID = "00001101-0000-1000-8000-00805F9B34FB"


def build_transport(name: str, port: int, com: str | None = None, sdp_mode: str = "auto") -> Port:
    if name == "tcp":
        return TcpPort(host="0.0.0.0", port=port)
    if name == "rfcomm":
        return RfcommPort(sdp_mode=sdp_mode)
    if name == "serial":
        if not com:
            raise ValueError("--com is required when --transport serial")
        return SerialPort(com_port=com)
    raise ValueError(f"unsupported transport: {name}")


def run_doctor() -> None:
    """Run Bluetooth RFCOMM diagnostics and print PASS/FAIL/UNKNOWN
    lines. Imported lazily so `--doctor` never has to build a transport
    or the Tkinter viewer."""
    from transport import diagnostics

    checks = [
        ("Bluetooth service: bthserv", lambda: diagnostics.check_windows_service("bthserv")),
        ("Bluetooth service: BTAGService", lambda: diagnostics.check_windows_service("BTAGService")),
        ("RFCOMM socket bind", diagnostics.check_rfcomm_socket_bind),
        ("SDP registration", diagnostics.check_sdp_registration),
        ("PC discoverability", diagnostics.check_discoverability),
        ("Paired Bluetooth devices", diagnostics.list_paired_bluetooth_devices),
        ("Bluetooth COM ports", diagnostics.list_bluetooth_com_ports),
        ("Python interpreter restriction (Store/AppContainer)", diagnostics.check_interpreter_restriction),
    ]
    for label, check in checks:
        result = check()
        print(f"[{result.status}] {label}: {result.message}")


def run_inspect_sdp() -> None:
    """Run `python main.py --inspect-sdp`: enumerate what is actually
    published in the local Bluetooth SDP database. Imported lazily for
    the same reason as `run_doctor` -- never build a transport or the
    Tkinter viewer just to inspect SDP state."""
    from transport import sdp_inspector

    sdp_inspector.run_inspect_sdp()


def parse_codepages(value: str) -> set[int]:
    """Parse `--codepages`: "all" or a comma-separated list of ids."""
    if value.strip().lower() == "all":
        return set(CODEPAGE_MAP)
    return {int(item.strip()) for item in value.split(",") if item.strip()}


class _ByteDumper:
    """Append-safe raw byte capture for `--dump-bytes PATH`.

    Every real print becomes a permanent regression fixture: bytes are
    appended (never overwritten) and flushed immediately after every
    write, so a crash before the process exits never loses the capture.
    Kept as its own small class (rather than a bare function) so the
    open file handle has an obvious owner and an explicit `close()`.
    """

    def __init__(self, path: str) -> None:
        self._file = open(path, "ab")

    def __call__(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._file.write(chunk)
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def _make_byte_dumper(path: str) -> _ByteDumper:
    return _ByteDumper(path)


class ReplayResult(NamedTuple):
    """Everything a fresh `Parser` produced from a replayed file.

    `replay_file()` used to return only `ops`, which silently discarded
    whatever the parser could not make sense of (see `take_diagnostics()`
    docstring) -- exactly the wrong place to lose that information, since
    replay is precisely the mode used to analyse a captured problem after
    the fact. Both callers (`run_replay()` for `--replay PATH` and
    `_make_open_handler()` for the Open button) must forward `diagnostics`
    to the viewer, not just `ops`.

    `byte_offsets` (from `Parser.take_op_offsets()`) is one entry per op
    in `ops`, giving the exact number of bytes each op was decoded from
    -- see `Viewer.update_ops()`'s `byte_offsets` parameter for why this
    matters for the session history panel's per-receipt byte counts.
    """

    ops: List[object]
    diagnostics: List[Diagnostic]
    byte_offsets: List[int]


def replay_file(path: str, implemented_codepages: Optional[Set[int]] = None) -> ReplayResult:
    """Read a captured byte file and feed it through a fresh `Parser`.

    Used by both `--replay PATH` (no transport, no window interaction
    beyond showing the result) and the Open button (loads a file into
    the already-running viewer). A fresh `Parser` per call mirrors what
    a fresh print job would see: no leftover style state from whatever
    was previously on screen.
    """
    with open(path, "rb") as f:
        data = f.read()
    parser = Parser(implemented_codepages=implemented_codepages)
    ops = parser.feed(data)
    diagnostics = parser.take_diagnostics()
    byte_offsets = parser.take_op_offsets()
    return ReplayResult(ops=ops, diagnostics=diagnostics, byte_offsets=byte_offsets)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="58mm/80mm ESC/POS thermal printer simulator")
    parser.add_argument(
        "--transport",
        choices=["tcp", "rfcomm", "serial"],
        default="tcp",
        help="transport adapter to use: tcp (default), rfcomm, or serial",
    )
    parser.add_argument("--port", type=int, default=9100, help="TCP port to listen on (--transport tcp)")
    parser.add_argument(
        "--com",
        default=None,
        help="COM port name for --transport serial (e.g. COM5)",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="run Bluetooth RFCOMM diagnostics and exit (does not start the viewer)",
    )
    parser.add_argument(
        "--inspect-sdp",
        action="store_true",
        help=(
            "enumerate and decode what is actually published in the local "
            "Bluetooth SDP database and exit (does not start the viewer)"
        ),
    )
    parser.add_argument(
        "--sdp-mode",
        choices=["auto", "simple", "blob"],
        default="auto",
        help=(
            "SDP registration path for --transport rfcomm: 'auto' (default) "
            "tries a hand-built record via lpBlob first and falls back to "
            "'simple' if that fails; 'simple' always uses the plain "
            "WSASetServiceW record Windows synthesizes; 'blob' always uses "
            "the hand-built record and never falls back"
        ),
    )
    parser.add_argument(
        "--width",
        type=int,
        choices=sorted(_WIDTH_DOTS_BY_MM),
        default=58,
        help="paper width in millimeters (58 or 80)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity",
    )
    parser.add_argument(
        "--codepages",
        default="all",
        help=(
            "comma-separated ESC/POS code page ids this printer profile "
            "implements (e.g. '0,2,16'), or 'all' for every id this "
            "simulator knows how to decode (default: all)"
        ),
    )
    parser.add_argument(
        "--dump-bytes",
        default=None,
        metavar="PATH",
        help=(
            "append every raw byte received to PATH as it arrives, so a "
            "real print becomes a permanent regression fixture for "
            "--replay (default: no capture)"
        ),
    )
    parser.add_argument(
        "--replay",
        default=None,
        metavar="PATH",
        help=(
            "render a previously captured byte file (see --dump-bytes) "
            "through a fresh parser and show it -- no transport is "
            "started, decoupling renderer iteration from real hardware"
        ),
    )
    args = parser.parse_args(argv)
    if args.transport == "serial" and not args.com:
        # A clean, actionable CLI error instead of a raw ValueError
        # traceback escaping run()/main() (build_transport() below still
        # raises ValueError for callers that invoke it directly).
        parser.error("--com is required when --transport serial")
    return args


def _transport_endpoint_label(
    args: argparse.Namespace,
    actual_channel: int | None = None,
    actual_port: int | None = None,
) -> str:
    """The bare endpoint identifier for the status bar/empty-state message
    (the transport name itself is shown separately there).

    For rfcomm the channel, and for tcp with `--port 0` the port, are only
    known once the transport has actually started -- `actual_channel`/
    `actual_port` are `None` before that."""
    if args.transport == "tcp":
        return f"port {actual_port if actual_port is not None else args.port}"
    if args.transport == "rfcomm":
        if actual_channel is not None:
            return f"channel {actual_channel}"
        return "channel (assigning...)"
    if args.transport == "serial":
        return args.com or ""
    return ""


def _transport_title(
    args: argparse.Namespace,
    actual_channel: int | None = None,
    actual_port: int | None = None,
) -> str:
    """Window-title fragment for the transport in use.

    `_transport_title()` used to be called only before `transport_port.start()`,
    so for rfcomm it could never know the real channel and fell back to a
    hardcoded, misleading "auto-assigned" string (the same blind spot applies
    to tcp when `--port 0` requests an OS-assigned ephemeral port).
    `actual_channel`/`actual_port` let the caller pass the real value (e.g.
    `RfcommPort.actual_channel`/`TcpPort.actual_port`) once the transport has
    started; `run()` calls this again after `start()` to refresh the title
    via `Viewer.set_title()`.
    """
    if args.transport == "tcp":
        return f"TCP :{actual_port if actual_port is not None else args.port}"
    if args.transport == "rfcomm":
        if actual_channel is not None:
            return f"RFCOMM (channel {actual_channel})"
        return "RFCOMM (channel pending)"
    if args.transport == "serial":
        return f"Serial {args.com}"
    return args.transport


class _StatusStore:
    """Thread-safe holder for the current `TransportStatus`.

    `on_data` and the connection-event listener built by
    `_make_connection_event_handler()` (both invoked from the transport's
    background thread) and `run()` (main thread, right after `start()`)
    all update the status independently; this serializes those updates
    and pushes each new snapshot to the viewer via `call_soon()` so
    Tkinter is only ever touched from its own thread.
    """

    def __init__(self, status: TransportStatus, viewer: Viewer) -> None:
        self._status = status
        self._viewer = viewer
        self._lock = threading.Lock()

    def update(self, transform: Callable[[TransportStatus], TransportStatus]) -> None:
        with self._lock:
            self._status = transform(self._status)
            status = self._status
        self._viewer.call_soon(self._viewer.update_status, status)


def _make_connection_event_handler(status_store: "_StatusStore") -> ConnectionListener:
    """Build the callback wired into `Port.set_connection_listener()`.

    Translates the transport-agnostic connection lifecycle events defined
    by `transport.port.ConnectionEvent` into `TransportStatus`
    transitions. This replaces the previous `_ConnectionEventBridge`
    design, which pattern-matched log message text through a
    `logging.Handler` -- a design that silently stopped updating the
    status bar whenever `--log-level` was raised above INFO. The
    listener fires regardless of logging configuration.

    Extracted as a standalone function (rather than a closure inline in
    `run()`) so it can be exercised directly in tests without needing a
    live Tkinter mainloop -- see tests/test_main.py.
    """

    def on_connection_event(event: ConnectionEvent, peer: Optional[str] = None) -> None:
        if event == "listening":
            status_store.update(lambda status: status.listening())
        elif event == "connected":
            status_store.update(lambda status: status.client_connected(peer))
        elif event == "disconnected":
            status_store.update(lambda status: status.client_disconnected())
        elif event == "stopped":
            status_store.update(lambda status: status.stopped())

    return on_connection_event


def _make_open_handler(
    viewer: Viewer,
    implemented_codepages: Optional[Set[int]],
    status_store: Optional["_StatusStore"] = None,
) -> Callable[[str], None]:
    """Build the callback wired into `Viewer(on_open=...)`/
    `set_open_handler()`: read a captured byte file, feed it through a
    fresh `Parser`, and replace the viewer's current content with it.

    Shared by the live `run()` path (Open button, transport keeps
    running independently) and `run_replay()` (Open button loads a
    different capture while still not starting any transport).

    In live mode, the transport keeps running after the file is loaded,
    so it can go on producing its own diagnostics after this call
    returns. Tagging each of this file's diagnostics with a `source`
    (see `core.diagnostics.Diagnostic.source`) keeps them attributable to
    the opened file in the Details window, instead of silently blending
    into whatever the live transport reports next -- `Viewer.clear()`
    (called below) already wipes any diagnostics collected before the
    file was opened, but does nothing to disambiguate what is added
    afterwards.

    `status_store`: in live mode, `_StatusStore` is the single source of
    truth for the status bar -- every live chunk accumulates on top of
    its own internal snapshot (see `run()`'s `on_data`). Reading/writing
    `viewer.get_status()`/`viewer.update_status()` directly here would
    bypass that snapshot, so the next live chunk would transform a stale
    `_StatusStore` snapshot that never learned about this file's counts,
    silently discarding them. When `status_store` is given, accumulate
    through it instead, exactly like `on_data` does. `run_replay()` has
    no `_StatusStore` at all, so it omits this argument and this falls
    back to reading/writing the viewer's status directly.
    """

    def on_open(path: str) -> None:
        result = replay_file(path, implemented_codepages)
        chunk_bytes = os.path.getsize(path)
        source = f"opened file: {os.path.basename(path)}"
        tagged_diagnostics = [dataclasses.replace(d, source=source) for d in result.diagnostics]
        viewer.call_soon(viewer.clear)
        viewer.call_soon(viewer.update_ops, result.ops, chunk_bytes, result.byte_offsets)
        if tagged_diagnostics:
            viewer.call_soon(viewer.add_diagnostics, tagged_diagnostics)
        # The status bar's byte/op counters are independent of the
        # receipt/history panel state that viewer.clear() just wiped --
        # they accumulate cumulatively across the whole session (live or
        # replay), exactly like live mode's _StatusStore.on_data().
        if status_store is not None:
            status_store.update(lambda status: status.add_bytes(chunk_bytes).add_ops(len(result.ops)))
        else:
            get_status = getattr(viewer, "get_status", None)
            current_status = get_status() if get_status is not None else None
            if current_status is not None:
                new_status = current_status.add_bytes(chunk_bytes).add_ops(len(result.ops))
                viewer.call_soon(viewer.update_status, new_status)

    return on_open


def run_replay(args: argparse.Namespace, width_dots: int, implemented_codepages: Set[int]) -> None:
    """Handle `--replay PATH`: render a previously captured byte file
    through a fresh parser and show it. No transport is built or
    started -- this decouples renderer iteration from real hardware
    entirely.
    """
    result = replay_file(args.replay, implemented_codepages)
    chunk_bytes = os.path.getsize(args.replay)
    status = TransportStatus(
        transport=args.transport,
        endpoint=_transport_endpoint_label(args),
        width_mm=args.width,
        codepages_label=args.codepages,
        replay_path=args.replay,
    ).add_bytes(chunk_bytes).add_ops(len(result.ops))
    viewer = Viewer(
        width_dots=width_dots,
        title=f"58mm Simulator - Replay: {args.replay}",
        status=status,
    )
    viewer.set_open_handler(_make_open_handler(viewer, implemented_codepages))

    logger.info("replay mode: showing %s (no transport started)", args.replay)
    viewer.update_ops(result.ops, chunk_bytes=chunk_bytes, byte_offsets=result.byte_offsets)
    if result.diagnostics:
        viewer.add_diagnostics(result.diagnostics)
    viewer.run()


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.doctor:
        run_doctor()
        return

    if args.inspect_sdp:
        run_inspect_sdp()
        return

    width_dots = _WIDTH_DOTS_BY_MM[args.width]
    implemented_codepages = parse_codepages(args.codepages)

    if args.replay:
        run_replay(args, width_dots, implemented_codepages)
        return

    parser = Parser(implemented_codepages=implemented_codepages)

    dumper: Optional[_ByteDumper] = None
    if args.dump_bytes:
        dumper = _make_byte_dumper(args.dump_bytes)
        logger.info("dumping every received byte to %s", args.dump_bytes)

    initial_status = TransportStatus(
        transport=args.transport,
        endpoint=_transport_endpoint_label(args),
        width_mm=args.width,
        codepages_label=args.codepages,
        sdp_uuid=_SPP_SERVICE_UUID if args.transport == "rfcomm" else None,
    )
    viewer = Viewer(
        width_dots=width_dots,
        title=f"58mm Simulator - {_transport_title(args)}",
        status=initial_status,
    )
    transport_port = build_transport(args.transport, args.port, args.com, sdp_mode=args.sdp_mode)
    status_store = _StatusStore(initial_status, viewer)

    def on_clear() -> None:
        # Clear ("new paper") only resets the viewer's own ops/receipts/
        # diagnostics (see Viewer.clear()); the byte/op counters shown in
        # the status bar are owned by _StatusStore, so resetting them is
        # this composition-root's job, not render/'s.
        status_store.update(lambda status: status.reset_counters())

    viewer.set_clear_handler(on_clear)
    viewer.set_open_handler(_make_open_handler(viewer, implemented_codepages, status_store=status_store))

    def on_data(chunk: bytes) -> None:
        if dumper is not None:
            dumper(chunk)
        ops = parser.feed(chunk)
        diagnostics = parser.take_diagnostics()
        byte_offsets = parser.take_op_offsets()
        status_store.update(lambda status: status.add_bytes(len(chunk)).add_ops(len(ops)))
        # Tkinter is not thread-safe: hop back onto the main/UI thread
        # before touching any widget from the transport's background
        # accept/read thread.
        if ops:
            viewer.call_soon(viewer.update_ops, ops, len(chunk), byte_offsets)
        if diagnostics:
            viewer.call_soon(viewer.add_diagnostics, diagnostics)

    transport_port.set_connection_listener(_make_connection_event_handler(status_store))

    transport_port.start(on_data)
    actual_channel = getattr(transport_port, "actual_channel", None)
    actual_port = getattr(transport_port, "actual_port", None)
    real_title = f"58mm Simulator - {_transport_title(args, actual_channel, actual_port)}"
    real_endpoint = _transport_endpoint_label(args, actual_channel, actual_port)
    viewer.call_soon(viewer.set_title, real_title)
    status_store.update(lambda status: status.listening().with_endpoint(real_endpoint))

    logger.info(
        "listening for ESC/POS bytes over %s (%s), paper width %dmm (%d dots)",
        args.transport,
        _transport_title(args, actual_channel, actual_port),
        args.width,
        width_dots,
    )

    try:
        viewer.run()
    finally:
        transport_port.stop()
        if dumper is not None:
            dumper.close()


if __name__ == "__main__":
    run()
