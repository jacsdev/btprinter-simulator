"""Tests for transport.tcp.TcpPort.

Uses real loopback TCP sockets on an OS-assigned ephemeral port (port 0)
so the tests never collide with a real run of main.py on 9100.
"""

import logging
import socket
import threading
import time

from transport.tcp import TcpPort


def _connect(port: int, timeout: float = 2.0) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    return client


def test_tcp_port_delivers_received_bytes_to_callback():
    received = []
    got_data = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        got_data.set()

    port = TcpPort(host="127.0.0.1", port=0)
    try:
        port.start(on_data)
        client = _connect(port.actual_port)
        try:
            client.sendall(b"\x1b\x40HELLO\x0a")
            assert got_data.wait(timeout=2.0)
        finally:
            client.close()
    finally:
        port.stop()

    combined = b"".join(received)
    assert b"HELLO" in combined


def test_tcp_port_handles_bytes_split_across_multiple_sends():
    received = []
    lock = threading.Lock()
    got_all = threading.Event()

    def on_data(chunk: bytes) -> None:
        with lock:
            received.append(chunk)
            if b"".join(received) == b"\x1b\x61\x01":
                got_all.set()

    port = TcpPort(host="127.0.0.1", port=0)
    try:
        port.start(on_data)
        client = _connect(port.actual_port)
        try:
            client.sendall(bytes([0x1B]))
            time.sleep(0.05)
            client.sendall(bytes([0x61]))
            time.sleep(0.05)
            client.sendall(bytes([0x01]))
            assert got_all.wait(timeout=2.0)
        finally:
            client.close()
    finally:
        port.stop()


def test_tcp_port_stop_releases_the_listening_socket():
    port = TcpPort(host="127.0.0.1", port=0)
    port.start(lambda chunk: None)
    bound_port = port.actual_port

    port.stop()

    # The OS should let us rebind almost immediately after a clean close.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", bound_port))
    finally:
        probe.close()


# -- connection event listener --------------------------------------------
#
# TcpPort must report its connection lifecycle through the Port base
# class's listener hook (see transport/port.py), not only through log
# records -- that dependency on log text is exactly the defect being
# fixed here.


def test_tcp_port_emits_listening_connected_disconnected_and_stopped_events():
    events = []
    disconnected = threading.Event()

    def on_event(event, peer=None):
        events.append((event, peer))
        if event == "disconnected":
            disconnected.set()

    port = TcpPort(host="127.0.0.1", port=0)
    port.set_connection_listener(on_event)

    port.start(lambda chunk: None)
    try:
        client = _connect(port.actual_port)
        client.sendall(b"x")
        client.close()
        assert disconnected.wait(timeout=2.0)
    finally:
        port.stop()

    event_names = [event for event, _peer in events]
    assert event_names == ["listening", "connected", "disconnected", "stopped"]
    connected_peer = events[1][1]
    assert connected_peer is not None
    assert "127.0.0.1" in connected_peer


def test_tcp_port_connection_events_fire_even_when_the_logger_level_is_warning():
    # This is the specific regression the design fix addresses: status
    # used to be driven by pattern-matching "client connected from ..."
    # log text through a logging.Handler, which silently stopped working
    # once the logger's effective level was raised above INFO. The
    # listener hook must keep firing regardless of the logger's level.
    tcp_logger = logging.getLogger("btprinter.transport.tcp")
    previous_level = tcp_logger.level
    tcp_logger.setLevel(logging.WARNING)

    events = []
    got_connected = threading.Event()

    def on_event(event, peer=None):
        events.append((event, peer))
        if event == "connected":
            got_connected.set()

    port = TcpPort(host="127.0.0.1", port=0)
    port.set_connection_listener(on_event)

    try:
        port.start(lambda chunk: None)
        client = _connect(port.actual_port)
        try:
            assert got_connected.wait(timeout=2.0)
        finally:
            client.close()
    finally:
        port.stop()
        tcp_logger.setLevel(previous_level)

    assert ("listening", None) in [(e, p) for e, p in events]
    assert any(event == "connected" for event, _peer in events)


def test_tcp_port_listener_exception_does_not_kill_the_accept_read_loop():
    def raising_listener(event, peer=None):
        raise RuntimeError("listener boom")

    received = []
    got_data = threading.Event()

    def on_data(chunk: bytes) -> None:
        received.append(chunk)
        got_data.set()

    port = TcpPort(host="127.0.0.1", port=0)
    port.set_connection_listener(raising_listener)

    try:
        port.start(on_data)
        client = _connect(port.actual_port)
        try:
            client.sendall(b"HELLO")
            # The "connected" event's listener already raised by this
            # point (it fires as soon as accept() returns); the read loop
            # must still be alive and deliver bytes normally.
            assert got_data.wait(timeout=2.0)
        finally:
            client.close()
    finally:
        port.stop()  # must also not raise despite the "stopped" event's listener
