"""TCP transport adapter.

Stands in for the real Bluetooth Classic SPP/RFCOMM channel used by the
Flutter app (`print_bluetooth_thermal`) in production: a plain byte
pipe, accepted on a background thread so it never blocks the caller
(typically the Tkinter mainloop in `main.py`). Bytes are handed to the
`on_data` callback exactly as received, in whatever chunks the OS
socket layer happens to deliver them — the core parser is responsible
for reassembling commands split across chunks.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Optional

from transport.port import OnDataCallback, Port

logger = logging.getLogger("btprinter.transport.tcp")

_ACCEPT_POLL_INTERVAL = 0.5
_RECV_CHUNK_SIZE = 4096


class TcpPort(Port):
    """Listens on `host:port` and streams received bytes to `on_data`.

    Accepts connections sequentially (one active client at a time, which
    matches how a single physical printer connection behaves). Passing
    `port=0` binds to an OS-assigned ephemeral port, useful for tests and
    for running multiple simulator instances side by side.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9100) -> None:
        self._host = host
        self._port = port
        self._server_socket: Optional[socket.socket] = None
        self._client_socket: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._on_data: Optional[OnDataCallback] = None
        self._running = False

    @property
    def actual_port(self) -> int:
        """The port actually bound (resolves `port=0` to its OS-assigned value)."""
        if self._server_socket is None:
            return self._port
        return self._server_socket.getsockname()[1]

    def start(self, on_data: OnDataCallback) -> None:
        self._on_data = on_data
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(1)
        self._running = True
        logger.info("TCP listener started on %s:%d", self._host, self.actual_port)
        self._emit_connection_event("listening")
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        server = self._server_socket
        if server is None:
            return
        try:
            server.settimeout(_ACCEPT_POLL_INTERVAL)
        except OSError:
            # stop() may have already closed the socket concurrently,
            # right after start() spawned this thread. Nothing to do.
            return
        while self._running:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            logger.info("client connected from %s", addr)
            self._emit_connection_event("connected", peer=f"{addr[0]}:{addr[1]}")
            self._client_socket = conn
            self._read_loop(conn)

    def _read_loop(self, conn: socket.socket) -> None:
        conn.settimeout(_ACCEPT_POLL_INTERVAL)
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
        client = self._client_socket
        if client is None:
            return
        try:
            client.sendall(data)
        except OSError:
            logger.warning("failed to write to client socket, ignoring")

    def stop(self) -> None:
        self._running = False
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
        logger.info("TCP listener stopped")
        self._emit_connection_event("stopped")
