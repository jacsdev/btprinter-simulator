"""Serial (COM port) transport adapter -- fallback for slice 2.

Windows itself publishes the SPP SDP record for a "Bluetooth Serial
Port (incoming)" COM port once one is configured in Bluetooth settings,
which sidesteps the whole manual SDP registration dance in
`transport.rfcomm`. This adapter just reads raw bytes off that COM
port via `pyserial`.

`pyserial` is imported lazily (inside `start()`, not at module import
time) so that importing this module -- and therefore the rest of the
transport package -- never fails when pyserial isn't installed. Only
actually starting this adapter without pyserial present raises, with an
actionable message.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from transport.port import OnDataCallback, Port

logger = logging.getLogger("btprinter.transport.serial")

_READ_TIMEOUT = 0.5  # seconds; also doubles as the _running poll interval
_RECV_CHUNK_SIZE = 4096
_DEFAULT_IDLE_TIMEOUT = 3.0  # seconds of silence before returning to "listening"

_SerialFactory = Callable[[], object]


class SerialPort(Port):
    """Reads bytes from a Windows incoming Bluetooth Serial COM port.

    `_serial_factory` is injectable so the start/stop/read lifecycle can
    be unit tested against a fake serial-like object without pyserial
    installed and without real hardware -- see tests/test_serialport.py.
    """

    def __init__(
        self,
        com_port: str,
        baudrate: int = 9600,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
        _serial_factory: Optional[_SerialFactory] = None,
    ) -> None:
        self._com_port = com_port
        self._baudrate = baudrate
        self._idle_timeout = idle_timeout
        self._serial_factory = _serial_factory
        self._serial = None
        self._read_thread: Optional[threading.Thread] = None
        self._on_data: Optional[OnDataCallback] = None
        self._running = False
        # Tracks the only thing this adapter can actually observe: whether
        # bytes are currently flowing. See _read_loop()'s "listening" /
        # "data_flowing" transitions below.
        self._data_flowing = False
        self._last_byte_time: Optional[float] = None

    def start(self, on_data: OnDataCallback) -> None:
        self._on_data = on_data
        factory = self._serial_factory or self._default_serial_factory()
        self._serial = factory()
        self._running = True
        self._data_flowing = False
        self._last_byte_time = None
        logger.info("Serial listener started on %s", self._com_port)
        # Unlike tcp/rfcomm, a Bluetooth "incoming" COM port has no
        # accept/read boundary this adapter can observe: by the time
        # start() can open it, Windows has already paired and connected
        # the peer, and pyserial exposes no API here to report the peer
        # going away. So this adapter never claims "connected"/
        # "disconnected" -- it reports what it *can* observe instead:
        # byte flow. "listening" here means "open, no bytes flowing right
        # now"; _read_loop() moves to "data_flowing" once bytes arrive and
        # back to "listening" after `idle_timeout` seconds of silence.
        self._emit_connection_event("listening")
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

    def _default_serial_factory(self) -> _SerialFactory:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is required for the serial transport. "
                "Install it with: pip install pyserial"
            ) from exc
        return lambda: serial.Serial(
            self._com_port, baudrate=self._baudrate, timeout=_READ_TIMEOUT
        )

    def _read_loop(self) -> None:
        ser = self._serial
        if ser is None:
            return
        while self._running:
            try:
                chunk = ser.read(_RECV_CHUNK_SIZE)
            except Exception:
                logger.exception("serial read failed, stopping read loop")
                break
            now = time.monotonic()
            if not chunk:
                # A read timeout with nothing available; loop again so we
                # keep polling self._running without assuming any chunk
                # boundary beyond whatever pyserial handed back. If we were
                # previously flowing and it has been quiet for
                # idle_timeout seconds, report that honestly: back to
                # "listening", never "disconnected" -- this adapter cannot
                # tell a real disconnect from the peer simply going quiet.
                if (
                    self._data_flowing
                    and self._last_byte_time is not None
                    and (now - self._last_byte_time) >= self._idle_timeout
                ):
                    self._data_flowing = False
                    logger.info(
                        "no data for %.1fs on %s, returning to the waiting state",
                        self._idle_timeout,
                        self._com_port,
                    )
                    self._emit_connection_event("listening")
                continue
            if not self._data_flowing:
                self._data_flowing = True
                logger.info("data flowing on %s", self._com_port)
                self._emit_connection_event("data_flowing")
            self._last_byte_time = now
            if self._on_data is not None:
                try:
                    self._on_data(chunk)
                except Exception:
                    logger.exception("on_data callback raised, continuing")

    def write(self, data: bytes) -> None:
        """Best-effort write, never raises. Kept for interface symmetry;
        the real Flutter app never reads from the printer connection."""
        ser = self._serial
        if ser is None:
            return
        try:
            ser.write(data)
        except Exception:
            logger.warning("failed to write to serial port, ignoring")

    def stop(self) -> None:
        self._running = False
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        if self._read_thread is not None:
            self._read_thread.join(timeout=2.0)
            self._read_thread = None
        logger.info("Serial listener stopped")
        self._emit_connection_event("stopped")
