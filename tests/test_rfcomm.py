"""Tests for transport.rfcomm.RfcommPort and its Win32 SDP helpers.

Two different testing strategies are used here, deliberately:

- The pure parts (GUID/struct byte layout, register/deregister calling
  the right WSAESETSERVICEOP constant, error handling when
  WSASetServiceW fails) are tested for real, by either running the
  actual ctypes structures or by monkeypatching the `_ws2_32` module
  attribute that wraps the ws2_32.dll boundary.
- The adapter's start/stop/read lifecycle is tested against an
  injected fake socket (a loopback-style test double), because a real
  AF_BLUETOOTH RFCOMM connection needs a second, already-paired
  Bluetooth device to connect from -- there is no way to open a
  connection to yourself the way TCP loopback does. SDP visibility to
  a real phone cannot be verified by any unit test; see README.md
  "Manual verification required".
"""

from __future__ import annotations

import ctypes
import logging
import socket
import threading

import pytest

import transport.rfcomm as rfcomm
from transport.rfcomm import (
    AF_BLUETOOTH,
    BT_PORT_ANY,
    BTPROTO_RFCOMM,
    CSADDR_INFO,
    GUID,
    RfcommPort,
    SdpRegistrationError,
    SOCKADDR_BTH,
    WSAQUERYSETW,
    _bind_available_channel,
    deregister_sdp_service,
    deregister_sdp_service_blob,
    make_spp_guid,
    register_sdp_service,
    register_sdp_service_blob,
)
from transport.sdp_encoding import BLOB, BTH_SET_SERVICE, build_spp_sdp_record


# ---------------------------------------------------------------------------
# Fake socket test doubles (loopback-style, no real Bluetooth radio needed)
# ---------------------------------------------------------------------------


class _FakeClientSocket:
    """Stands in for the socket returned by a real server socket's accept()."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        pass

    def recv(self, size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""  # simulate a clean disconnect (EOF)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


class _FakeServerSocket:
    """Stands in for the AF_BLUETOOTH/BTPROTO_RFCOMM listening socket."""

    def __init__(self, channel: int, clients: list[_FakeClientSocket]) -> None:
        self._channel = channel
        self._clients = list(clients)
        self.closed = False
        self.close_raises: Exception | None = None

    def bind(self, addr: tuple[str, int]) -> None:
        pass

    def listen(self, backlog: int) -> None:
        pass

    def settimeout(self, timeout: float) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        return ("00:00:00:00:00:00", self._channel)

    def accept(self):
        if self._clients:
            client = self._clients.pop(0)
            return client, ("AA:BB:CC:DD:EE:FF", 0)
        raise socket.timeout()

    def close(self) -> None:
        self.closed = True
        if self.close_raises is not None:
            raise self.close_raises


def _noop_register(channel: int, name: str) -> None:
    pass


def _noop_deregister(channel: int, name: str) -> None:
    pass


class _FakeScanningServerSocket:
    """Models the real quirk discovered on this machine: binding to port
    0 succeeds but never assigns a real channel, so callers must fall
    back to scanning explicit channel numbers. `bind_outcomes` maps a
    requested channel to either `None` (bind succeeds) or an `OSError`
    instance to raise (bind fails, e.g. reserved/in use)."""

    def __init__(self, bind_outcomes: dict[int, "OSError | None"]) -> None:
        self._bind_outcomes = bind_outcomes
        self._bound_channel: int | None = None
        self.closed = False

    def bind(self, addr: tuple[str, int]) -> None:
        channel = addr[1]
        outcome = self._bind_outcomes.get(channel, OSError("channel not available"))
        if outcome is not None:
            raise outcome
        self._bound_channel = channel

    def listen(self, backlog: int) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        # Mirrors the real quirk: port 0 "binds" but is never resolved
        # to a real channel by getsockname().
        reported = self._bound_channel if self._bound_channel else 0
        return ("00:00:00:00:00:00", reported)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# GUID / struct byte layout -- real, asserting tests, no mocking
# ---------------------------------------------------------------------------


def test_spp_guid_data1_data2_data3_are_native_ints_matching_spp_uuid():
    guid = make_spp_guid()
    assert guid.Data1 == 0x00001101
    assert guid.Data2 == 0x0000
    assert guid.Data3 == 0x1000


def test_spp_guid_data4_matches_exact_byte_sequence_no_byteswap():
    guid = make_spp_guid()
    assert bytes(bytearray(guid.Data4)) == bytes(
        [0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB]
    )


def test_guid_struct_size_is_16_bytes():
    assert ctypes.sizeof(GUID) == 16


def test_sockaddr_bth_fields_are_settable_and_readable():
    addr = SOCKADDR_BTH()
    addr.addressFamily = AF_BLUETOOTH
    addr.btAddr = 0
    addr.serviceClassId = make_spp_guid()
    addr.port = 7
    assert addr.addressFamily == AF_BLUETOOTH
    assert addr.port == 7
    assert addr.serviceClassId.Data1 == 0x00001101


def test_csaddr_info_fields_are_settable_and_readable():
    info = CSADDR_INFO()
    info.iSocketType = socket.SOCK_STREAM
    info.iProtocol = BTPROTO_RFCOMM
    assert info.iSocketType == socket.SOCK_STREAM
    assert info.iProtocol == BTPROTO_RFCOMM


def test_wsaqueryset_dwsize_field_matches_struct_size():
    query_set = WSAQUERYSETW()
    query_set.dwSize = ctypes.sizeof(WSAQUERYSETW)
    assert query_set.dwSize == ctypes.sizeof(WSAQUERYSETW)


# ---------------------------------------------------------------------------
# WSAStartup -- real call, safe and idempotent on this machine
# ---------------------------------------------------------------------------


def test_wsa_startup_succeeds_on_this_machine():
    assert rfcomm._wsa_startup() == 0


# ---------------------------------------------------------------------------
# register/deregister -- mocked ws2_32 boundary
# ---------------------------------------------------------------------------


class _FakeWs2_32Success:
    def __init__(self) -> None:
        self.operations: list[int] = []

    def WSASetServiceW(self, query_set_ptr, operation, flags) -> int:
        self.operations.append(operation)
        return 0

    def WSAGetLastError(self) -> int:
        return 0


class _FakeWs2_32Failure:
    def __init__(self, error_code: int) -> None:
        self._error_code = error_code

    def WSASetServiceW(self, query_set_ptr, operation, flags) -> int:
        return -1  # SOCKET_ERROR

    def WSAGetLastError(self) -> int:
        return self._error_code


def test_register_sdp_service_calls_wsasetservicew_with_register_op(monkeypatch):
    fake = _FakeWs2_32Success()
    monkeypatch.setattr(rfcomm, "_ws2_32", fake)
    register_sdp_service(5, "test service")
    assert fake.operations == [rfcomm.RNRSERVICE_REGISTER]


def test_deregister_sdp_service_calls_wsasetservicew_with_delete_op(monkeypatch):
    fake = _FakeWs2_32Success()
    monkeypatch.setattr(rfcomm, "_ws2_32", fake)
    deregister_sdp_service(5, "test service")
    assert fake.operations == [rfcomm.RNRSERVICE_DELETE]


def test_register_sdp_service_raises_with_win32_error_code_on_failure(monkeypatch):
    monkeypatch.setattr(rfcomm, "_ws2_32", _FakeWs2_32Failure(1231))
    with pytest.raises(SdpRegistrationError) as excinfo:
        register_sdp_service(5, "test service")
    assert excinfo.value.win32_error_code == 1231


def test_deregister_sdp_service_raises_with_win32_error_code_on_failure(monkeypatch):
    monkeypatch.setattr(rfcomm, "_ws2_32", _FakeWs2_32Failure(1232))
    with pytest.raises(SdpRegistrationError) as excinfo:
        deregister_sdp_service(5, "test service")
    assert excinfo.value.win32_error_code == 1232


# ---------------------------------------------------------------------------
# lpBlob registration path (--sdp-mode blob) -- mocked ws2_32 boundary.
# The fake below reads back the real WSAQUERYSETW/BLOB/BTH_SET_SERVICE
# chain the same way ws2_32.dll would, so these tests assert the *actual*
# bytes register_sdp_service_blob hands to Windows, not just that some
# call happened.
# ---------------------------------------------------------------------------


class _FakeWs2_32BlobSuccess:
    def __init__(self, assigned_handle: int = 4242) -> None:
        self.assigned_handle = assigned_handle
        self.captured_operations: list[int] = []
        self.captured_record_bytes: bytes | None = None

    def WSASetServiceW(self, query_set_ptr, operation, flags) -> int:
        self.captured_operations.append(operation)
        query_set = ctypes.cast(query_set_ptr, ctypes.POINTER(WSAQUERYSETW)).contents
        blob = ctypes.cast(query_set.lpBlob, ctypes.POINTER(BLOB)).contents
        buffer_address = ctypes.addressof(blob.pBlobData.contents)
        set_service = BTH_SET_SERVICE.from_address(buffer_address)
        record_offset = BTH_SET_SERVICE.pRecord.offset
        self.captured_record_bytes = ctypes.string_at(
            buffer_address + record_offset, set_service.ulRecordLength
        )
        if operation == rfcomm.RNRSERVICE_REGISTER:
            # Simulate the OS writing the assigned record handle back into
            # *pRecordHandle, the way a real registration would.
            set_service.pRecordHandle.contents.value = self.assigned_handle
        return 0

    def WSAGetLastError(self) -> int:
        return 0


class _FakeWs2_32BlobFailure:
    def __init__(self, error_code: int) -> None:
        self._error_code = error_code

    def WSASetServiceW(self, query_set_ptr, operation, flags) -> int:
        return -1  # SOCKET_ERROR

    def WSAGetLastError(self) -> int:
        return self._error_code


def test_register_sdp_service_blob_calls_wsasetservicew_with_register_op(monkeypatch):
    fake = _FakeWs2_32BlobSuccess()
    monkeypatch.setattr(rfcomm, "_ws2_32", fake)
    register_sdp_service_blob(4, "Test Service")
    assert fake.captured_operations == [rfcomm.RNRSERVICE_REGISTER]


def test_register_sdp_service_blob_sends_the_exact_hand_built_sdp_record_bytes(monkeypatch):
    fake = _FakeWs2_32BlobSuccess()
    monkeypatch.setattr(rfcomm, "_ws2_32", fake)
    register_sdp_service_blob(4, "Test Service")
    assert fake.captured_record_bytes == build_spp_sdp_record(4, "Test Service")


def test_register_sdp_service_blob_returns_the_handle_the_os_writes_back(monkeypatch):
    fake = _FakeWs2_32BlobSuccess(assigned_handle=9999)
    monkeypatch.setattr(rfcomm, "_ws2_32", fake)
    handle = register_sdp_service_blob(4, "Test Service")
    assert handle == 9999


def test_register_sdp_service_blob_raises_with_win32_error_code_on_failure(monkeypatch):
    monkeypatch.setattr(rfcomm, "_ws2_32", _FakeWs2_32BlobFailure(1231))
    with pytest.raises(SdpRegistrationError) as excinfo:
        register_sdp_service_blob(4, "Test Service")
    assert excinfo.value.win32_error_code == 1231


def test_deregister_sdp_service_blob_calls_wsasetservicew_with_delete_op(monkeypatch):
    fake = _FakeWs2_32BlobSuccess()
    monkeypatch.setattr(rfcomm, "_ws2_32", fake)
    deregister_sdp_service_blob(4242)
    assert fake.captured_operations == [rfcomm.RNRSERVICE_DELETE]


def test_deregister_sdp_service_blob_is_a_safe_no_op_for_a_falsy_handle(monkeypatch):
    fake = _FakeWs2_32BlobSuccess()
    monkeypatch.setattr(rfcomm, "_ws2_32", fake)
    deregister_sdp_service_blob(0)
    assert fake.captured_operations == []  # WSASetServiceW must not be called at all


def test_deregister_sdp_service_blob_raises_with_win32_error_code_on_failure(monkeypatch):
    monkeypatch.setattr(rfcomm, "_ws2_32", _FakeWs2_32BlobFailure(1232))
    with pytest.raises(SdpRegistrationError) as excinfo:
        deregister_sdp_service_blob(4242)
    assert excinfo.value.win32_error_code == 1232


# ---------------------------------------------------------------------------
# RfcommPort --sdp-mode selection and fallback logic (auto/simple/blob).
# See the SdpMode docstring in transport/rfcomm.py for the documented
# behavior contract this section pins down.
# ---------------------------------------------------------------------------


def test_sdp_mode_simple_never_attempts_blob_registration():
    simple_calls = []

    def spy_simple(channel: int, name: str) -> None:
        simple_calls.append(channel)

    def blob_should_not_be_called(channel: int, name: str) -> int:
        raise AssertionError("blob registration must not be attempted in 'simple' mode")

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=spy_simple,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
        _register_sdp_blob=blob_should_not_be_called,
    )
    port.start(lambda chunk: None)
    try:
        assert simple_calls == [4]
        assert port._sdp_mode_used == "simple"
    finally:
        port.stop()


def test_sdp_mode_blob_registers_via_blob_path_only():
    blob_calls = []

    def spy_blob(channel: int, name: str) -> int:
        blob_calls.append(channel)
        return 55

    def simple_should_not_be_called(channel: int, name: str) -> None:
        raise AssertionError("simple registration must not be attempted in 'blob' mode")

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=simple_should_not_be_called,
        _deregister_sdp=_noop_deregister,
        sdp_mode="blob",
        _register_sdp_blob=spy_blob,
        _deregister_sdp_blob=lambda handle: None,
    )
    port.start(lambda chunk: None)
    try:
        assert blob_calls == [4]
        assert port._sdp_mode_used == "blob"
    finally:
        port.stop()


def test_sdp_mode_blob_does_not_fall_back_to_simple_on_failure():
    simple_calls = []

    def spy_simple(channel: int, name: str) -> None:
        simple_calls.append(channel)

    def failing_blob(channel: int, name: str) -> int:
        raise SdpRegistrationError(1231, "blob register")

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=spy_simple,
        _deregister_sdp=_noop_deregister,
        sdp_mode="blob",
        _register_sdp_blob=failing_blob,
    )
    with pytest.raises(SdpRegistrationError):
        port.start(lambda chunk: None)
    assert simple_calls == []  # no fallback attempted
    assert server.closed is True


def test_sdp_mode_auto_uses_blob_when_it_succeeds():
    blob_calls = []
    simple_calls = []

    def spy_blob(channel: int, name: str) -> int:
        blob_calls.append(channel)
        return 77

    def spy_simple(channel: int, name: str) -> None:
        simple_calls.append(channel)

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=spy_simple,
        _deregister_sdp=_noop_deregister,
        sdp_mode="auto",
        _register_sdp_blob=spy_blob,
        _deregister_sdp_blob=lambda handle: None,
    )
    port.start(lambda chunk: None)
    try:
        assert blob_calls == [4]
        assert simple_calls == []
        assert port._sdp_mode_used == "blob"
    finally:
        port.stop()


def test_sdp_mode_auto_falls_back_to_simple_when_blob_registration_fails():
    simple_calls = []

    def failing_blob(channel: int, name: str) -> int:
        raise SdpRegistrationError(1231, "blob register")

    def spy_simple(channel: int, name: str) -> None:
        simple_calls.append(channel)

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=spy_simple,
        _deregister_sdp=_noop_deregister,
        sdp_mode="auto",
        _register_sdp_blob=failing_blob,
    )
    port.start(lambda chunk: None)  # must NOT raise -- auto falls back
    try:
        assert simple_calls == [4]
        assert port._sdp_mode_used == "simple"
    finally:
        port.stop()


def test_stop_deregisters_via_blob_path_when_blob_mode_was_used():
    blob_dereg_calls = []
    simple_dereg_calls = []

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=lambda channel, name: simple_dereg_calls.append(channel),
        sdp_mode="blob",
        _register_sdp_blob=lambda channel, name: 88,
        _deregister_sdp_blob=lambda handle: blob_dereg_calls.append(handle),
    )
    port.start(lambda chunk: None)
    port.stop()

    assert blob_dereg_calls == [88]
    assert simple_dereg_calls == []


def test_stop_deregisters_via_simple_path_when_auto_fell_back_to_simple():
    blob_dereg_calls = []
    simple_dereg_calls = []

    def failing_blob(channel: int, name: str) -> int:
        raise SdpRegistrationError(1231, "blob register")

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=lambda channel, name: simple_dereg_calls.append(channel),
        sdp_mode="auto",
        _register_sdp_blob=failing_blob,
        _deregister_sdp_blob=lambda handle: blob_dereg_calls.append(handle),
    )
    port.start(lambda chunk: None)
    port.stop()

    assert simple_dereg_calls == [4]
    assert blob_dereg_calls == []


# ---------------------------------------------------------------------------
# Channel selection -- port-0 fast path and the real-machine scan fallback
# ---------------------------------------------------------------------------


def test_bind_available_channel_returns_immediately_when_port_zero_works():
    server = _FakeServerSocket(channel=13, clients=[])
    bound_socket, channel = _bind_available_channel(lambda: server)
    assert bound_socket is server
    assert channel == 13


def test_bind_available_channel_falls_back_to_scanning_when_port_zero_does_not_assign():
    # Channel 0 "binds" without ever reporting a real channel (the quirk
    # this machine exhibits); channel 4 is refused (simulating a
    # reserved/in-use channel); channel 5 succeeds.
    outcomes = {
        0: None,
        4: OSError("simulated WinError 10013"),
        5: None,
    }
    created_sockets: list[_FakeScanningServerSocket] = []

    def factory() -> _FakeScanningServerSocket:
        sock = _FakeScanningServerSocket(outcomes)
        created_sockets.append(sock)
        return sock

    bound_socket, channel = _bind_available_channel(factory)

    assert channel == 5
    assert bound_socket.getsockname() == ("00:00:00:00:00:00", 5)
    # The port-0 probe and the failed channel-4 attempt must both have
    # been closed; only the winning socket stays open.
    assert created_sockets[0].closed is True  # port 0 probe
    assert created_sockets[1].closed is True  # channel 4, refused
    assert created_sockets[2] is bound_socket
    assert created_sockets[2].closed is False


def test_bind_available_channel_raises_when_no_channel_in_range_is_available():
    outcomes = {0: None}  # every explicit channel is refused by default

    def factory() -> _FakeScanningServerSocket:
        return _FakeScanningServerSocket(outcomes)

    with pytest.raises(RuntimeError):
        _bind_available_channel(factory)


def test_bind_available_channel_falls_back_to_scanning_when_port_zero_probe_raises_oserror():
    # Some drivers refuse the port-0 probe outright (bind()/listen() raise
    # OSError) instead of the "binds but never assigns a channel" quirk
    # covered above. The probe socket must still be closed, and the scan
    # fallback must still run instead of letting the OSError escape.
    outcomes = {
        0: OSError("simulated bind failure on port 0 probe"),
        4: OSError("simulated WinError 10013"),
        5: None,
    }
    created_sockets: list[_FakeScanningServerSocket] = []

    def factory() -> _FakeScanningServerSocket:
        sock = _FakeScanningServerSocket(outcomes)
        created_sockets.append(sock)
        return sock

    bound_socket, channel = _bind_available_channel(factory)

    assert channel == 5
    assert created_sockets[0].closed is True  # port 0 probe, bind raised
    assert created_sockets[1].closed is True  # channel 4, refused
    assert created_sockets[2] is bound_socket
    assert created_sockets[2].closed is False


def test_bind_available_channel_raises_runtime_error_when_port_zero_probe_raises_and_scan_finds_nothing():
    outcomes = {0: OSError("simulated bind failure on port 0 probe")}

    def factory() -> _FakeScanningServerSocket:
        return _FakeScanningServerSocket(outcomes)

    with pytest.raises(RuntimeError):
        _bind_available_channel(factory)


# ---------------------------------------------------------------------------
# RfcommPort lifecycle -- fake socket, no real Bluetooth radio needed
# ---------------------------------------------------------------------------


def test_rfcomm_port_delivers_received_bytes_to_callback():
    received: list[bytes] = []
    got_data = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        got_data.set()

    client = _FakeClientSocket([b"\x1b\x40HELLO\x0a"])
    server = _FakeServerSocket(channel=7, clients=[client])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    try:
        port.start(on_data)
        assert got_data.wait(timeout=2.0)
    finally:
        port.stop()

    assert b"HELLO" in b"".join(received)


def test_rfcomm_port_delivers_chunks_exactly_as_received_and_in_order():
    received: list[bytes] = []
    got_all = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        if b"".join(received) == b"\x1b\x61\x01":
            got_all.set()

    client = _FakeClientSocket([bytes([0x1B]), bytes([0x61]), bytes([0x01])])
    server = _FakeServerSocket(channel=9, clients=[client])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    try:
        port.start(on_data)
        assert got_all.wait(timeout=2.0)
    finally:
        port.stop()

    assert received == [bytes([0x1B]), bytes([0x61]), bytes([0x01])]


def test_actual_channel_reports_the_assigned_channel():
    server = _FakeServerSocket(channel=11, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    port.start(lambda chunk: None)
    try:
        assert port.actual_channel == 11
    finally:
        port.stop()


def test_start_registers_sdp_with_the_actually_bound_channel():
    registered = []

    def spy_register(channel: int, name: str) -> None:
        registered.append(channel)

    server = _FakeServerSocket(channel=42, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=spy_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    port.start(lambda chunk: None)
    try:
        assert registered == [42]
    finally:
        port.stop()


def test_start_raises_and_closes_socket_when_sdp_registration_fails():
    def failing_register(channel: int, name: str) -> None:
        raise SdpRegistrationError(1231, "register")

    server = _FakeServerSocket(channel=4, clients=[])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=failing_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    with pytest.raises(SdpRegistrationError):
        port.start(lambda chunk: None)
    assert server.closed is True


def test_stop_deregisters_sdp_even_if_closing_the_socket_fails():
    dereg_calls = []

    def spy_deregister(channel: int, name: str) -> None:
        dereg_calls.append(channel)

    server = _FakeServerSocket(channel=3, clients=[])
    server.close_raises = OSError("simulated close failure")
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=spy_deregister,
        sdp_mode="simple",
    )
    port.start(lambda chunk: None)
    port.stop()  # must not raise despite the simulated close failure

    assert dereg_calls == [3]


def test_write_without_connected_peer_is_a_safe_no_op():
    port = RfcommPort(
        _socket_factory=lambda: _FakeServerSocket(channel=1, clients=[]),
        _register_sdp=_noop_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    port.write(b"anything")  # no start() called, no peer -- must not raise


def test_write_forwards_bytes_to_the_connected_client_socket():
    port = RfcommPort()
    fake_client = _FakeClientSocket([])
    port._client_socket = fake_client
    port.write(b"ack")
    assert fake_client.sent == [b"ack"]


def test_write_swallows_oserror_from_the_client_socket():
    class _RaisingClient:
        def sendall(self, data: bytes) -> None:
            raise OSError("no peer")

    port = RfcommPort()
    port._client_socket = _RaisingClient()
    port.write(b"x")  # must not raise


# ---------------------------------------------------------------------------
# Connection event listener -- reports lifecycle via the Port base class's
# listener hook (transport/port.py), not by logging text a caller has to
# pattern-match. See test_main.py for why that used to be the design.
# ---------------------------------------------------------------------------


def test_rfcomm_port_emits_listening_connected_disconnected_and_stopped_events():
    events = []
    got_disconnected = threading.Event()

    def on_event(event, peer=None):
        events.append((event, peer))
        if event == "disconnected":
            got_disconnected.set()

    client = _FakeClientSocket([b"HELLO", b""])  # b"" -> clean disconnect
    server = _FakeServerSocket(channel=7, clients=[client])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    port.set_connection_listener(on_event)

    port.start(lambda chunk: None)
    try:
        assert got_disconnected.wait(timeout=2.0)
    finally:
        port.stop()

    event_names = [event for event, _peer in events]
    assert event_names == ["listening", "connected", "disconnected", "stopped"]
    assert events[1][1] == "AA:BB:CC:DD:EE:FF"


def test_rfcomm_port_connection_events_fire_even_when_the_logger_level_is_warning():
    rfcomm_logger = logging.getLogger("btprinter.transport.rfcomm")
    previous_level = rfcomm_logger.level
    rfcomm_logger.setLevel(logging.WARNING)

    events = []
    got_connected = threading.Event()

    def on_event(event, peer=None):
        events.append((event, peer))
        if event == "connected":
            got_connected.set()

    client = _FakeClientSocket([b"HELLO"])
    server = _FakeServerSocket(channel=8, clients=[client])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    port.set_connection_listener(on_event)

    try:
        port.start(lambda chunk: None)
        assert got_connected.wait(timeout=2.0)
    finally:
        port.stop()
        rfcomm_logger.setLevel(previous_level)

    assert any(event == "listening" for event, _peer in events)
    assert any(event == "connected" for event, _peer in events)


def test_rfcomm_port_listener_exception_does_not_kill_the_accept_read_loop():
    def raising_listener(event, peer=None):
        raise RuntimeError("listener boom")

    received: list[bytes] = []
    got_data = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        got_data.set()

    client = _FakeClientSocket([b"HELLO"])
    server = _FakeServerSocket(channel=9, clients=[client])
    port = RfcommPort(
        _socket_factory=lambda: server,
        _register_sdp=_noop_register,
        _deregister_sdp=_noop_deregister,
        sdp_mode="simple",
    )
    port.set_connection_listener(raising_listener)

    try:
        port.start(on_data)
        assert got_data.wait(timeout=2.0)
    finally:
        port.stop()  # must not raise despite the "stopped" event's listener

    assert b"HELLO" in b"".join(received)


# ---------------------------------------------------------------------------
# Real hardware check -- skips honestly if no Bluetooth radio is present
# ---------------------------------------------------------------------------


def test_real_af_bluetooth_socket_can_be_created():
    try:
        probe = socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
    except OSError as exc:
        pytest.skip(f"AF_BLUETOOTH/BTPROTO_RFCOMM unavailable on this machine: {exc}")
    probe.close()


def test_real_bind_port_zero_does_not_assign_a_channel_on_this_machine():
    """Documents the real, verified behavior of this development
    machine's Bluetooth stack: bind()/listen() to BT_PORT_ANY (0)
    succeed without error, but getsockname() keeps reporting channel 0
    afterwards -- it does NOT auto-assign a channel the way Microsoft's
    SOCKADDR_BTH documentation describes. This is why
    _bind_available_channel() exists. If this ever starts failing (i.e.
    a real channel > 0 comes back), the scanning fallback can be
    simplified or removed -- update this test and the module docstring
    together.
    """
    try:
        probe = socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
    except OSError as exc:
        pytest.skip(f"AF_BLUETOOTH/BTPROTO_RFCOMM unavailable on this machine: {exc}")
    try:
        probe.bind(("00:00:00:00:00:00", BT_PORT_ANY))
        probe.listen(1)
        channel = probe.getsockname()[1]
        assert channel == 0
    finally:
        probe.close()


def test_real_bind_available_channel_finds_a_real_channel_on_this_machine():
    try:
        socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM).close()
    except OSError as exc:
        pytest.skip(f"AF_BLUETOOTH/BTPROTO_RFCOMM unavailable on this machine: {exc}")
    bound_socket, channel = _bind_available_channel(rfcomm._default_rfcomm_socket)
    try:
        assert 1 <= channel <= 30
        assert bound_socket.getsockname()[1] == channel
    finally:
        bound_socket.close()
