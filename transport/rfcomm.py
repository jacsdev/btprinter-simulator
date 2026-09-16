"""Bluetooth Classic SPP/RFCOMM transport adapter (Windows only).

A bare `AF_BLUETOOTH`/`BTPROTO_RFCOMM` server socket is invisible to a
phone's SDP (Service Discovery Protocol) lookup on its own -- Python's
`socket` module never publishes an SDP record. This module additionally
registers one via `ctypes` calls into `ws2_32.dll`'s `WSASetServiceW`,
which is the same mechanism the Windows Bluetooth stack itself uses.

The registered service class ID is the well-known Serial Port Profile
(SPP) UUID `00001101-0000-1000-8000-00805F9B34FB`, which is what
`createRfcommSocketToServiceRecord` on the Android side looks up.

Struct layouts below are transcribed from the documented Win32 headers
(`ws2bth.h` for `SOCKADDR_BTH`, `ws2def.h`/`nspapi.h` for
`SOCKET_ADDRESS`/`CSADDR_INFO`, `winsock2.h` for `WSAQUERYSETW`). They
have not been independently confirmed by a successful phone pairing on
this development machine -- see the field-by-field confidence notes on
each struct, and README.md's "Manual verification required" section.

Channel assignment correction (discovered while implementing this
module, contradicts an earlier assumption): Microsoft's documentation
for `SOCKADDR_BTH.port` states that binding to 0 (`BT_PORT_ANY`) lets
the system assign a free RFCOMM channel automatically. On this
development machine that is NOT what happens -- `bind()` and `listen()`
both succeed with port 0, but `getsockname()` keeps reporting channel 0
afterwards, and even a real `WSASetServiceW(RNRSERVICE_REGISTER)` call
with port 0 "succeeds" (returns 0) without ever writing back a real
channel into the SOCKADDR_BTH passed to it. Publishing an SDP record
for channel 0 would be meaningless to a real client. `_bind_available_channel`
below tries port 0 first (in case a different driver/stack does honor
it), and falls back to scanning explicit channel numbers when it does
not, verified for real on this machine (see the final report / engram
topic `btprinter-simulator/rfcomm-port-zero-does-not-autoassign`).
"""

from __future__ import annotations

import ctypes
import logging
import socket
import threading
from typing import Callable, Literal, Optional

from transport import sdp_encoding
from transport.port import OnDataCallback, Port

logger = logging.getLogger("btprinter.transport.rfcomm")

_ACCEPT_POLL_INTERVAL = 0.5
_RECV_CHUNK_SIZE = 4096

DEFAULT_SERVICE_NAME = "58mm Thermal Printer Simulator"

# Confirmed on this machine (see prior feasibility testing): AF_BLUETOOTH
# and BTPROTO_RFCOMM exist as socket module constants with these values.
AF_BLUETOOTH = socket.AF_BLUETOOTH
BTPROTO_RFCOMM = socket.BTPROTO_RFCOMM
BT_PORT_ANY = 0  # bind() to this and read the assigned channel back via getsockname()

# winsock2.h: typedef enum _WSAESETSERVICEOP { RNRSERVICE_REGISTER = 0,
# RNRSERVICE_DEREGISTER = 1, RNRSERVICE_DELETE = 2 } WSAESETSERVICEOP;
RNRSERVICE_REGISTER = 0
RNRSERVICE_DELETE = 2

NS_BTH = 16  # ws2bth.h: NS_BTH namespace id for the Bluetooth NS provider

_WSA_VERSION_2_2 = 0x0202  # MAKEWORD(2, 2)

_SOCK_STREAM = 1
_BTHPROTO_RFCOMM = 3


class SdpRegistrationError(RuntimeError):
    """Raised when WSASetServiceW fails to register or deregister the SDP
    record. Always carries the real Win32 error code from
    WSAGetLastError() -- never swallowed or guessed."""

    def __init__(self, win32_error_code: int, operation: str) -> None:
        self.win32_error_code = win32_error_code
        self.operation = operation
        super().__init__(
            f"WSASetServiceW failed during SDP {operation}: "
            f"Win32 error {win32_error_code}"
        )


# ---------------------------------------------------------------------------
# Win32 structs
#
# ws2_32 is loaded once at import time; individual calls are wrapped in
# module-level functions (_wsa_startup, register_sdp_service,
# deregister_sdp_service) so tests can monkeypatch `_ws2_32` itself to
# exercise the error-handling path without touching real Winsock state.
# ---------------------------------------------------------------------------

_ws2_32 = ctypes.WinDLL("ws2_32.dll")


class GUID(ctypes.Structure):
    """Confidence: high. Standard COM/Win32 GUID layout, used unchanged
    across the whole Win32 API surface."""

    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def make_spp_guid() -> GUID:
    """Build the well-known Serial Port Profile UUID
    00001101-0000-1000-8000-00805F9B34FB as a GUID struct.

    Data1/Data2/Data3 are plain native-endian ints in memory (ctypes
    already stores them correctly for the local machine's endianness).
    Data4 is the UUID's trailing 8 bytes in their natural order -- it
    must NOT be byte-swapped.
    """
    return GUID(
        0x00001101,
        0x0000,
        0x1000,
        (ctypes.c_ubyte * 8)(0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB),
    )


class SOCKADDR_BTH(ctypes.Structure):
    """Confidence: high. Matches ws2bth.h's SOCKADDR_BTH, widely used in
    published Bluetooth-over-Winsock sample code (addressFamily as
    USHORT, btAddr as a 64-bit BTH_ADDR, an embedded service class GUID,
    then a ULONG port/channel)."""

    _fields_ = [
        ("addressFamily", ctypes.c_ushort),
        ("btAddr", ctypes.c_ulonglong),
        ("serviceClassId", GUID),
        ("port", ctypes.c_ulong),
    ]


class SOCKET_ADDRESS(ctypes.Structure):
    """Confidence: high. Standard nspapi.h SOCKET_ADDRESS: a pointer to a
    sockaddr plus its length, used identically across getaddrinfo and
    the WSA namespace provider APIs."""

    _fields_ = [
        ("lpSockaddr", ctypes.POINTER(SOCKADDR_BTH)),
        ("iSockaddrLength", ctypes.c_int),
    ]


class CSADDR_INFO(ctypes.Structure):
    """Confidence: high. Standard nspapi.h CSADDR_INFO layout."""

    _fields_ = [
        ("LocalAddr", SOCKET_ADDRESS),
        ("RemoteAddr", SOCKET_ADDRESS),
        ("iSocketType", ctypes.c_int),
        ("iProtocol", ctypes.c_int),
    ]


class WSAQUERYSETW(ctypes.Structure):
    """Confidence: medium-high, NOT independently confirmed on this
    machine. This layout is transcribed from the documented winsock2.h
    WSAQUERYSETW (the field order/types below match the structure as
    published in the Windows SDK headers and in widely-cited Bluetooth
    SDP registration sample code), but no successful end-to-end SDP
    registration observed by a real Android device has been performed
    here to independently verify field offsets. If a future run shows
    WSASetServiceW returning WSAEFAULT/WSAEINVAL, re-check this layout
    first.
    """

    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("lpszServiceInstanceName", ctypes.c_wchar_p),
        ("lpServiceClassId", ctypes.POINTER(GUID)),
        ("lpVersion", ctypes.c_void_p),
        ("lpszComment", ctypes.c_wchar_p),
        ("dwNameSpace", ctypes.c_ulong),
        ("lpNSProviderId", ctypes.POINTER(GUID)),
        ("lpszContext", ctypes.c_wchar_p),
        ("dwNumberOfProtocols", ctypes.c_ulong),
        ("lpafpProtocols", ctypes.c_void_p),
        ("lpszQueryString", ctypes.c_wchar_p),
        ("dwNumberOfCsAddrs", ctypes.c_ulong),
        ("lpcsaBuffer", ctypes.POINTER(CSADDR_INFO)),
        ("dwOutputFlags", ctypes.c_ulong),
        ("lpBlob", ctypes.c_void_p),
    ]


class WSADATA(ctypes.Structure):
    """Confidence: high for the 64-bit field order used here (confirmed
    this process is 64-bit via ctypes.sizeof(c_void_p) == 8). WSADATA
    has a different field order on 32-bit Windows; this module only
    targets the 64-bit layout since that's what this project runs on."""

    _fields_ = [
        ("wVersion", ctypes.c_ushort),
        ("wHighVersion", ctypes.c_ushort),
        ("iMaxSockets", ctypes.c_ushort),
        ("iMaxUdpDg", ctypes.c_ushort),
        ("lpVendorInfo", ctypes.c_char_p),
        ("szDescription", ctypes.c_char * 257),
        ("szSystemStatus", ctypes.c_char * 129),
    ]


def _wsa_startup() -> int:
    """Call WSAStartup(MAKEWORD(2, 2), ...). Safe to call more than once
    per process (Winsock reference-counts successful calls); this module
    never calls the matching WSACleanup, since CPython's own `socket`
    module owns that lifecycle for the process."""
    wsa_data = WSADATA()
    return _ws2_32.WSAStartup(_WSA_VERSION_2_2, ctypes.byref(wsa_data))


def _build_query_set(channel: int, service_name: str):
    """Build the WSAQUERYSETW (and the structs it points into) needed to
    register or deregister the SPP SDP record for `channel`. Returns the
    query set together with the structs it references, so the caller can
    keep them alive for the duration of the WSASetServiceW call."""
    service_class_id = make_spp_guid()

    local_addr = SOCKADDR_BTH(
        AF_BLUETOOTH,
        0,  # btAddr: 0 ("any") -- only the channel matters for SDP publication
        service_class_id,
        channel,
    )

    csaddr = CSADDR_INFO()
    ctypes.memset(ctypes.byref(csaddr), 0, ctypes.sizeof(csaddr))
    csaddr.LocalAddr.lpSockaddr = ctypes.pointer(local_addr)
    csaddr.LocalAddr.iSockaddrLength = ctypes.sizeof(SOCKADDR_BTH)
    csaddr.iSocketType = _SOCK_STREAM
    csaddr.iProtocol = _BTHPROTO_RFCOMM

    query_set = WSAQUERYSETW()
    ctypes.memset(ctypes.byref(query_set), 0, ctypes.sizeof(query_set))
    query_set.dwSize = ctypes.sizeof(WSAQUERYSETW)
    query_set.lpszServiceInstanceName = service_name
    query_set.lpServiceClassId = ctypes.pointer(service_class_id)
    query_set.dwNameSpace = NS_BTH
    query_set.dwNumberOfCsAddrs = 1
    query_set.lpcsaBuffer = ctypes.pointer(csaddr)

    # Keep every referenced struct alive for as long as query_set is used.
    return query_set, service_class_id, local_addr, csaddr


def register_sdp_service(channel: int, service_name: str = DEFAULT_SERVICE_NAME) -> None:
    """Publish an SDP record advertising the SPP UUID on `channel`, the
    channel actually assigned by bind() -- never a hardcoded value."""
    query_set, _service_class_id, _local_addr, _csaddr = _build_query_set(channel, service_name)
    result = _ws2_32.WSASetServiceW(ctypes.byref(query_set), RNRSERVICE_REGISTER, 0)
    if result != 0:
        error_code = _ws2_32.WSAGetLastError()
        raise SdpRegistrationError(error_code, "register")


def deregister_sdp_service(channel: int, service_name: str = DEFAULT_SERVICE_NAME) -> None:
    """Remove the SDP record previously published for `channel`."""
    query_set, _service_class_id, _local_addr, _csaddr = _build_query_set(channel, service_name)
    result = _ws2_32.WSASetServiceW(ctypes.byref(query_set), RNRSERVICE_DELETE, 0)
    if result != 0:
        error_code = _ws2_32.WSAGetLastError()
        raise SdpRegistrationError(error_code, "deregister")


# ---------------------------------------------------------------------------
# lpBlob registration path (--sdp-mode blob) -- registers a hand-built SDP
# record (transport/sdp_encoding.build_spp_sdp_record) via
# WSAQUERYSETW.lpBlob/BTH_SET_SERVICE instead of letting Windows synthesize
# a minimal record from the plain WSASetServiceW call above. Per Microsoft's
# documentation for WSAQUERYSET ("Bluetooth and WSASetService"), when
# lpBlob is provided every other WSAQUERYSETW member except dwSize and
# dwNameSpace is ignored.
# ---------------------------------------------------------------------------


def register_sdp_service_blob(channel: int, service_name: str = DEFAULT_SERVICE_NAME) -> int:
    """Publish a hand-built SDP record for `channel` via
    `WSAQUERYSETW.lpBlob`. Returns the SDP record handle written back into
    `BTH_SET_SERVICE.pRecordHandle`, which `deregister_sdp_service_blob`
    needs later to remove the record. Raises `SdpRegistrationError` (with
    the real `WSAGetLastError()` code) if `WSASetServiceW` fails."""
    sdp_record = sdp_encoding.build_spp_sdp_record(channel, service_name)
    record_length = len(sdp_record)

    # BTH_SET_SERVICE ends in a flexible `UCHAR pRecord[1]` array: allocate
    # a raw buffer sized for the fixed header plus the real record length
    # (one byte less than sizeof(BTH_SET_SERVICE) is already accounted for
    # by pRecord's declared first byte).
    buffer_size = ctypes.sizeof(sdp_encoding.BTH_SET_SERVICE) - 1 + record_length
    raw_buffer = ctypes.create_string_buffer(buffer_size)
    set_service = ctypes.cast(raw_buffer, ctypes.POINTER(sdp_encoding.BTH_SET_SERVICE)).contents

    sdp_version = ctypes.c_ulong(sdp_encoding.BTH_SDP_VERSION)
    record_handle = ctypes.c_void_p(0)  # must be NULL for new registration
    set_service.pSdpVersion = ctypes.pointer(sdp_version)
    set_service.pRecordHandle = ctypes.pointer(record_handle)
    set_service.fCodService = 0
    set_service.ulRecordLength = record_length
    ctypes.memmove(
        ctypes.addressof(raw_buffer) + sdp_encoding.BTH_SET_SERVICE.pRecord.offset,
        sdp_record,
        record_length,
    )

    blob = sdp_encoding.BLOB()
    blob.cbSize = buffer_size
    blob.pBlobData = ctypes.cast(raw_buffer, ctypes.POINTER(ctypes.c_ubyte))

    query_set = WSAQUERYSETW()
    ctypes.memset(ctypes.byref(query_set), 0, ctypes.sizeof(query_set))
    query_set.dwSize = ctypes.sizeof(WSAQUERYSETW)
    query_set.dwNameSpace = NS_BTH
    query_set.lpBlob = ctypes.cast(ctypes.byref(blob), ctypes.c_void_p)

    result = _ws2_32.WSASetServiceW(ctypes.byref(query_set), RNRSERVICE_REGISTER, 0)
    if result != 0:
        error_code = _ws2_32.WSAGetLastError()
        raise SdpRegistrationError(error_code, "blob register")
    return record_handle.value or 0


def deregister_sdp_service_blob(record_handle: int) -> None:
    """Remove the SDP record previously published by
    `register_sdp_service_blob`, identified by the handle it returned. A
    falsy `record_handle` (e.g. 0, meaning registration never actually
    produced one) is a safe no-op -- there is nothing to delete."""
    if not record_handle:
        return
    buffer_size = ctypes.sizeof(sdp_encoding.BTH_SET_SERVICE)
    raw_buffer = ctypes.create_string_buffer(buffer_size)
    set_service = ctypes.cast(raw_buffer, ctypes.POINTER(sdp_encoding.BTH_SET_SERVICE)).contents

    sdp_version = ctypes.c_ulong(sdp_encoding.BTH_SDP_VERSION)
    handle_value = ctypes.c_void_p(record_handle)
    set_service.pSdpVersion = ctypes.pointer(sdp_version)
    set_service.pRecordHandle = ctypes.pointer(handle_value)
    set_service.ulRecordLength = 0

    blob = sdp_encoding.BLOB()
    blob.cbSize = buffer_size
    blob.pBlobData = ctypes.cast(raw_buffer, ctypes.POINTER(ctypes.c_ubyte))

    query_set = WSAQUERYSETW()
    ctypes.memset(ctypes.byref(query_set), 0, ctypes.sizeof(query_set))
    query_set.dwSize = ctypes.sizeof(WSAQUERYSETW)
    query_set.dwNameSpace = NS_BTH
    query_set.lpBlob = ctypes.cast(ctypes.byref(blob), ctypes.c_void_p)

    result = _ws2_32.WSASetServiceW(ctypes.byref(query_set), RNRSERVICE_DELETE, 0)
    if result != 0:
        error_code = _ws2_32.WSAGetLastError()
        raise SdpRegistrationError(error_code, "blob deregister")


def _default_rfcomm_socket() -> socket.socket:
    return socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)


_SocketFactory = Callable[[], socket.socket]
_SdpOperation = Callable[[int, str], None]
_SdpBlobRegisterOperation = Callable[[int, str], int]
_SdpBlobDeregisterOperation = Callable[[int], None]

SdpMode = Literal["auto", "simple", "blob"]
"""Which SDP registration path RfcommPort.start() uses, selected via
`--sdp-mode`:

- "simple": always use `register_sdp_service` (plain WSASetServiceW,
  letting Windows synthesize the record). Never attempts blob, never
  falls back.
- "blob": always use `register_sdp_service_blob` (hand-built record via
  lpBlob). Deliberately does NOT fall back to "simple" on failure --
  this mode exists specifically to test/debug the blob path itself, so
  silently falling back would hide the failure this mode is meant to
  surface.
- "auto" (default): try "blob" first; if it raises for any reason, log
  the failure at INFO level and fall back to "simple". This is the mode
  real runs should use, since a working blob record is strictly better
  (see transport/sdp_encoding.py) but must never make the simulator
  refuse to start on a Windows build where the blob path fails.
"""

# RFCOMM channels are numbered 1-30. Channels 1 and 2 raise
# PermissionError (WinError 10013, reserved by Windows BT services) and
# channel 3 raises WinError 10048 (in use) on this machine, so the scan
# starts at 4. See the module docstring for why this scan exists at all.
_CHANNEL_SCAN_START = 4
_CHANNEL_SCAN_END = 30


def _bind_available_channel(socket_factory: _SocketFactory):
    """Bind a fresh RFCOMM socket to an available channel, returning
    `(bound_socket, channel)`. Tries BT_PORT_ANY (0) first; falls back to
    scanning explicit channel numbers if that binds/listens without
    error but never actually assigns a channel (see module docstring).
    """
    probe = socket_factory()
    try:
        probe.bind(("00:00:00:00:00:00", BT_PORT_ANY))
        probe.listen(1)
    except OSError:
        try:
            probe.close()
        except OSError:
            pass
    else:
        channel = probe.getsockname()[1]
        if channel > 0:
            return probe, channel
        try:
            probe.close()
        except OSError:
            pass

    last_error: Optional[OSError] = None
    for candidate in range(_CHANNEL_SCAN_START, _CHANNEL_SCAN_END + 1):
        candidate_socket = socket_factory()
        try:
            candidate_socket.bind(("00:00:00:00:00:00", candidate))
            candidate_socket.listen(1)
            return candidate_socket, candidate
        except OSError as exc:
            last_error = exc
            try:
                candidate_socket.close()
            except OSError:
                pass
    raise RuntimeError(
        f"could not bind any RFCOMM channel in range "
        f"{_CHANNEL_SCAN_START}-{_CHANNEL_SCAN_END}: {last_error}"
    )


class RfcommPort(Port):
    """Listens on an OS-assigned RFCOMM channel and streams received bytes
    to `on_data`, publishing an SDP record so Android's SDP client can
    find the service by the SPP UUID.

    The socket and SDP register/deregister calls are all injectable
    (`_socket_factory`, `_register_sdp`, `_deregister_sdp`) so the
    start/stop/read lifecycle can be unit tested against a fake socket
    without a real Bluetooth radio or a paired peer -- see
    tests/test_rfcomm.py.
    """

    def __init__(
        self,
        service_name: str = DEFAULT_SERVICE_NAME,
        _socket_factory: Optional[_SocketFactory] = None,
        _register_sdp: Optional[_SdpOperation] = None,
        _deregister_sdp: Optional[_SdpOperation] = None,
        sdp_mode: SdpMode = "auto",
        _register_sdp_blob: Optional[_SdpBlobRegisterOperation] = None,
        _deregister_sdp_blob: Optional[_SdpBlobDeregisterOperation] = None,
    ) -> None:
        self._service_name = service_name
        self._socket_factory = _socket_factory or _default_rfcomm_socket
        self._register_sdp = _register_sdp or register_sdp_service
        self._deregister_sdp = _deregister_sdp or deregister_sdp_service
        self._sdp_mode: SdpMode = sdp_mode
        self._register_sdp_blob = _register_sdp_blob or register_sdp_service_blob
        self._deregister_sdp_blob = _deregister_sdp_blob or deregister_sdp_service_blob
        self._server_socket = None
        self._client_socket = None
        self._accept_thread: Optional[threading.Thread] = None
        self._on_data: Optional[OnDataCallback] = None
        self._running = False
        self._sdp_registered = False
        self._sdp_mode_used: Optional[str] = None
        self._blob_record_handle: Optional[int] = None

    @property
    def actual_channel(self) -> int:
        """The RFCOMM channel actually assigned by bind() (resolves
        BT_PORT_ANY to its OS-assigned value)."""
        if self._server_socket is None:
            return 0
        return self._server_socket.getsockname()[1]

    def start(self, on_data: OnDataCallback) -> None:
        self._on_data = on_data
        _wsa_startup()
        server, channel = _bind_available_channel(self._socket_factory)
        self._server_socket = server
        try:
            self._register_sdp_for_mode(channel)
        except Exception:
            # Any failure during SDP registration - not just the expected
            # SdpRegistrationError - must not leak the bound server socket.
            logger.exception("SDP registration failed, aborting start")
            try:
                server.close()
            except OSError:
                pass
            self._server_socket = None
            raise
        self._sdp_registered = True
        self._running = True
        logger.info(
            "RFCOMM listener started on channel %d, SDP service '%s' registered (mode: %s)",
            channel,
            self._service_name,
            self._sdp_mode_used,
        )
        self._emit_connection_event("listening")
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _register_sdp_for_mode(self, channel: int) -> None:
        """Register the SDP record using the path selected by
        `self._sdp_mode` (see the `SdpMode` docstring above for the
        "auto"/"simple"/"blob" behavior contract)."""
        if self._sdp_mode == "simple":
            self._register_sdp(channel, self._service_name)
            self._sdp_mode_used = "simple"
            logger.info("SDP registration mode: simple (--sdp-mode simple)")
            return
        if self._sdp_mode == "blob":
            self._blob_record_handle = self._register_sdp_blob(channel, self._service_name)
            self._sdp_mode_used = "blob"
            logger.info("SDP registration mode: blob (--sdp-mode blob)")
            return
        # "auto": try the hand-built blob record first (see
        # transport/sdp_encoding.py for why it is strictly more complete
        # than the simple path), falling back to "simple" so a Windows
        # build where the blob path fails still starts the simulator.
        try:
            self._blob_record_handle = self._register_sdp_blob(channel, self._service_name)
            self._sdp_mode_used = "blob"
            logger.info("SDP registration mode: blob (auto: blob registration succeeded)")
        except Exception as exc:
            logger.info(
                "SDP registration mode: simple (auto: blob registration failed, falling back: %s)",
                exc,
            )
            self._register_sdp(channel, self._service_name)
            self._sdp_mode_used = "simple"

    def _accept_loop(self) -> None:
        server = self._server_socket
        if server is None:
            return
        try:
            server.settimeout(_ACCEPT_POLL_INTERVAL)
        except OSError:
            return
        while self._running:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            logger.info("client connected from %s", addr)
            self._emit_connection_event("connected", peer=str(addr[0]))
            self._client_socket = conn
            self._read_loop(conn)

    def _read_loop(self, conn) -> None:
        try:
            conn.settimeout(_ACCEPT_POLL_INTERVAL)
        except OSError:
            pass
        while self._running:
            try:
                chunk = conn.recv(_RECV_CHUNK_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                logger.info("client disconnected")
                self._emit_connection_event("disconnected")
                break
            if self._on_data is not None:
                try:
                    self._on_data(chunk)
                except Exception:
                    logger.exception("on_data callback raised, continuing")
        try:
            conn.close()
        except OSError:
            pass
        if self._client_socket is conn:
            self._client_socket = None

    def write(self, data: bytes) -> None:
        """Best-effort write, never raises. The real Flutter app only
        ever writes to the printer and never reads, so nothing in
        production actually calls this against a real peer."""
        client = self._client_socket
        if client is None:
            return
        try:
            client.sendall(data)
        except OSError:
            logger.warning("failed to write to client socket, ignoring")

    def stop(self) -> None:
        self._running = False
        try:
            if self._sdp_registered:
                channel = self.actual_channel
                try:
                    if self._sdp_mode_used == "blob" and self._blob_record_handle:
                        self._deregister_sdp_blob(self._blob_record_handle)
                    else:
                        self._deregister_sdp(channel, self._service_name)
                except SdpRegistrationError:
                    logger.exception(
                        "SDP deregistration failed, the record may remain "
                        "stale until the Bluetooth Support Service restarts"
                    )
                finally:
                    self._sdp_registered = False
                    self._sdp_mode_used = None
                    self._blob_record_handle = None
        finally:
            self._cleanup_sockets()
        logger.info("RFCOMM listener stopped")
        self._emit_connection_event("stopped")

    def _cleanup_sockets(self) -> None:
        if self._client_socket is not None:
            try:
                self._client_socket.close()
            except OSError:
                pass
            self._client_socket = None
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
