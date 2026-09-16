"""Tests for the transport.Port interface.

Port is duplex-capable in shape (start/stop/write) even though slice 1
only ever uses the receive direction — the real mobile app's package has
no read method, so nothing ever calls write() against a real printer.
Keeping the shape duplex is cheap and avoids a breaking interface change
in slice 2 (Bluetooth Classic SPP/RFCOMM).
"""

import pytest

from transport.port import Port


def test_port_is_abstract_and_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Port()


def test_concrete_port_implementing_the_interface_works():
    class DummyPort(Port):
        def __init__(self):
            self.started_with = None
            self.written = []

        def start(self, on_data):
            self.started_with = on_data

        def stop(self):
            self.started_with = None

        def write(self, data: bytes) -> None:
            self.written.append(data)

    port = DummyPort()
    port.start(lambda chunk: None)
    port.write(b"abc")
    port.stop()

    assert port.written == [b"abc"]
    assert port.started_with is None


# -- connection event listener -------------------------------------------
#
# The connection listener is a base-class concern shared by every
# adapter (tcp/rfcomm/serialport), not something each one reimplements:
# a missing listener must be a safe no-op, and an exception raised by a
# registered listener must never escape `_emit_connection_event()` and
# break the caller's accept/read loop.


class _EmittingPort(Port):
    """Minimal concrete Port used only to exercise the connection
    listener plumbing declared on the abstract base class."""

    def start(self, on_data):
        pass

    def stop(self):
        pass

    def write(self, data: bytes) -> None:
        pass


def test_emit_connection_event_without_a_listener_is_a_safe_no_op():
    port = _EmittingPort()
    port._emit_connection_event("listening")  # must not raise


def test_set_connection_listener_receives_emitted_events():
    events = []
    port = _EmittingPort()
    port.set_connection_listener(lambda event, peer=None: events.append((event, peer)))

    port._emit_connection_event("listening")
    port._emit_connection_event("connected", peer="00:11:22:33:44:55")
    port._emit_connection_event("disconnected")
    port._emit_connection_event("stopped")

    assert events == [
        ("listening", None),
        ("connected", "00:11:22:33:44:55"),
        ("disconnected", None),
        ("stopped", None),
    ]


def test_set_connection_listener_with_none_clears_it_back_to_a_no_op():
    port = _EmittingPort()
    port.set_connection_listener(lambda event, peer=None: (_ for _ in ()).throw(AssertionError("should not be called")))

    port.set_connection_listener(None)

    port._emit_connection_event("listening")  # must not raise


def test_emit_connection_event_swallows_exceptions_raised_by_the_listener():
    def bad_listener(event, peer=None):
        raise RuntimeError("listener boom")

    port = _EmittingPort()
    port.set_connection_listener(bad_listener)

    port._emit_connection_event("connected", peer="X")  # must not raise
