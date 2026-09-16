"""Transport port interface.

A `Port` delivers raw bytes from whatever physical or logical channel
carries ESC/POS commands (TCP today, Bluetooth Classic SPP/RFCOMM in
slice 2) to the core parser, and nothing more. It never interprets the
bytes it moves.

The interface is duplex-capable in shape (`write` exists alongside
`start`/`stop`) even though slice 1 never calls `write` in anger: the
mobile app's `print_bluetooth_thermal` package has no read method, so a
real printer connection is write-only from the app's side. Keeping the
shape duplex now is cheap and avoids reshaping this interface when
slice 2 adds the Bluetooth adapter.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable, Literal, Optional

logger = logging.getLogger("btprinter.transport.port")

OnDataCallback = Callable[[bytes], None]

ConnectionEvent = Literal["listening", "connected", "disconnected", "stopped"]
"""The connection lifecycle events an adapter reports through its
connection listener (see `Port.set_connection_listener`):

- "listening": the transport is bound/open and waiting for a client
  (tcp/rfcomm), or the underlying channel has just been opened (serial).
- "connected": a client connected. `peer` carries a peer address string
  when the transport can supply one (tcp/rfcomm); `None` when it cannot
  (serial has no accept boundary to observe -- see transport/serialport.py).
- "disconnected": the previously connected client went away.
- "stopped": the transport has been shut down and released its resources.
"""

ConnectionListener = Callable[[ConnectionEvent, Optional[str]], None]


class Port(ABC):
    """Abstract duplex transport port."""

    _connection_listener: Optional[ConnectionListener] = None

    @abstractmethod
    def start(self, on_data: OnDataCallback) -> None:
        """Start listening/connecting, calling `on_data(chunk)` for every
        chunk of bytes received."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the port and release any underlying resources."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write bytes back to the connected peer, if any. Unused by any
        real 58mm printer app in slice 1, kept for interface symmetry."""

    def set_connection_listener(self, listener: Optional[ConnectionListener]) -> None:
        """Register a callback invoked with connection lifecycle events
        (see `ConnectionEvent`).

        The callback fires on whatever thread the adapter runs its
        accept/read loop on -- callers that need to touch single-threaded
        UI state (e.g. Tkinter) must hop back onto their own thread
        themselves; a `Port` never assumes anything about the caller's
        threading model.

        Passing `None` clears the listener. A `Port` with no listener
        registered never calls one: this is always a safe no-op, never
        an error, so adapters can freely fire events without checking
        whether anyone is listening.
        """
        self._connection_listener = listener

    def _emit_connection_event(self, event: ConnectionEvent, peer: Optional[str] = None) -> None:
        """Invoke the registered connection listener, if any.

        Never raises: an exception raised by the listener is caught and
        logged here so a misbehaving listener can never break the
        adapter's accept/read loop.
        """
        listener = self._connection_listener
        if listener is None:
            return
        try:
            listener(event, peer)
        except Exception:
            logger.exception("connection listener raised for event %r, continuing", event)
