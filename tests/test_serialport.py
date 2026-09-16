"""Tests for transport.serialport.SerialPort.

pyserial may or may not be installed in this environment, so the
"not installed" test simulates its absence explicitly rather than
depending on the environment -- an earlier version of it asserted the
package was genuinely missing and broke the moment pyserial was
installed for manual COM-port testing. The lifecycle/read-chunking
tests use an injected fake serial-like object (a loopback-style test
double); a real "Bluetooth Serial Port (incoming)" COM port cannot be
unit tested anyway.
"""

from __future__ import annotations

import sys
import threading

import pytest


def test_importing_serialport_module_succeeds_without_pyserial_installed():
    # pyserial is not installed in this environment; importing the
    # adapter module itself must not require it (lazy import).
    assert "serial" not in sys.modules or sys.modules["serial"] is None
    import transport.serialport  # noqa: F401 -- import success is the assertion


class _FakeSerial:
    """Stands in for a pyserial `Serial` instance."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.written: list[bytes] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""  # pyserial returns b"" on a read timeout with no data

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self.closed = True


def test_start_without_pyserial_installed_raises_actionable_error(monkeypatch):
    from transport.serialport import SerialPort

    # Simulate pyserial being absent regardless of whether it is actually
    # installed here. Setting the entry to None makes `import serial` raise
    # ImportError, which is exactly the condition the adapter must translate
    # into an actionable RuntimeError.
    monkeypatch.setitem(sys.modules, "serial", None)

    port = SerialPort(com_port="COM5")
    with pytest.raises(RuntimeError) as excinfo:
        port.start(lambda chunk: None)
    assert "pip install pyserial" in str(excinfo.value)


def test_serial_port_delivers_received_bytes_to_callback():
    from transport.serialport import SerialPort

    received: list[bytes] = []
    got_data = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        got_data.set()

    fake_serial = _FakeSerial([b"\x1b\x40HELLO\x0a"])
    port = SerialPort(com_port="COM5", _serial_factory=lambda: fake_serial)
    try:
        port.start(on_data)
        assert got_data.wait(timeout=2.0)
    finally:
        port.stop()

    assert b"HELLO" in b"".join(received)


def test_serial_port_delivers_chunks_exactly_as_read_without_assuming_boundaries():
    from transport.serialport import SerialPort

    received: list[bytes] = []
    got_all = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        if b"".join(received) == b"\x1b\x61\x01":
            got_all.set()

    fake_serial = _FakeSerial([bytes([0x1B, 0x61]), bytes([0x01])])
    port = SerialPort(com_port="COM5", _serial_factory=lambda: fake_serial)
    try:
        port.start(on_data)
        assert got_all.wait(timeout=2.0)
    finally:
        port.stop()

    assert received == [bytes([0x1B, 0x61]), bytes([0x01])]


def test_serial_port_stop_closes_serial_and_joins_thread():
    from transport.serialport import SerialPort

    fake_serial = _FakeSerial([])
    port = SerialPort(com_port="COM5", _serial_factory=lambda: fake_serial)
    port.start(lambda chunk: None)
    port.stop()

    assert fake_serial.closed is True
    assert port._read_thread is None


def test_write_forwards_bytes_to_the_open_serial_port():
    from transport.serialport import SerialPort

    fake_serial = _FakeSerial([])
    port = SerialPort(com_port="COM5", _serial_factory=lambda: fake_serial)
    port.start(lambda chunk: None)
    try:
        port.write(b"ack")
        assert fake_serial.written == [b"ack"]
    finally:
        port.stop()


def test_write_without_open_serial_port_is_a_safe_no_op():
    from transport.serialport import SerialPort

    port = SerialPort(com_port="COM5")
    port.write(b"anything")  # never started -- must not raise


def test_write_swallows_errors_from_the_serial_port():
    from transport.serialport import SerialPort

    class _RaisingSerial:
        def write(self, data: bytes) -> None:
            raise OSError("port unplugged")

        def close(self) -> None:
            pass

    port = SerialPort(com_port="COM5")
    port._serial = _RaisingSerial()
    port.write(b"x")  # must not raise


# ---------------------------------------------------------------------------
# Connection event listener -- reports lifecycle via the Port base class's
# listener hook (transport/port.py).
#
# Unlike tcp/rfcomm, a Bluetooth "incoming" COM port has no accept/read
# boundary this adapter can observe -- by the time `start()` can open it,
# Windows has already paired and connected the peer, and pyserial has no
# API here to report the peer going away. So this adapter only ever
# reports "listening" (COM port opened) and "stopped" (COM port closed),
# matching its behavior before this fix (it never had a "client
# connected"/"client disconnected" log message to begin with).
# ---------------------------------------------------------------------------


def test_serial_port_emits_listening_on_start_and_stopped_on_stop():
    from transport.serialport import SerialPort

    events = []
    fake_serial = _FakeSerial([])
    port = SerialPort(com_port="COM5", _serial_factory=lambda: fake_serial)
    port.set_connection_listener(lambda event, peer=None: events.append((event, peer)))

    port.start(lambda chunk: None)
    port.stop()

    assert events == [("listening", None), ("stopped", None)]


def test_serial_port_listener_exception_does_not_kill_the_read_loop():
    from transport.serialport import SerialPort

    def raising_listener(event, peer=None):
        raise RuntimeError("listener boom")

    received: list = []
    got_data = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        got_data.set()

    fake_serial = _FakeSerial([b"\x1b\x40HELLO\x0a"])
    port = SerialPort(com_port="COM5", _serial_factory=lambda: fake_serial)
    port.set_connection_listener(raising_listener)

    try:
        port.start(on_data)
        assert got_data.wait(timeout=2.0)
    finally:
        port.stop()  # must not raise despite the "stopped" event's listener

    assert b"HELLO" in b"".join(received)
