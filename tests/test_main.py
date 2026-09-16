"""Tests for main.py's CLI wiring: new transports, --com, and --doctor.

Most of these are unit tests of parse_args/build_transport/run_doctor in
isolation -- they never open a Tkinter window. One group below (the
_StatusStore/_make_connection_event_handler tests) does open a real
loopback TCP socket to prove the connection-event wiring is independent
of --log-level, but still never touches Tkinter -- a fake viewer stands
in for render.viewer.Viewer.
"""

from __future__ import annotations

import socket

import pytest

import main
from transport.diagnostics import CheckResult
from transport.rfcomm import RfcommPort
from transport.serialport import SerialPort
from transport.tcp import TcpPort


def test_parse_args_defaults_transport_to_tcp():
    args = main.parse_args([])
    assert args.transport == "tcp"
    assert args.doctor is False
    assert args.com is None


def test_parse_args_accepts_rfcomm_transport():
    args = main.parse_args(["--transport", "rfcomm"])
    assert args.transport == "rfcomm"


def test_parse_args_accepts_serial_transport_with_com_port():
    args = main.parse_args(["--transport", "serial", "--com", "COM5"])
    assert args.transport == "serial"
    assert args.com == "COM5"


def test_parse_args_accepts_doctor_flag():
    args = main.parse_args(["--doctor"])
    assert args.doctor is True


def test_parse_args_accepts_inspect_sdp_flag():
    args = main.parse_args(["--inspect-sdp"])
    assert args.inspect_sdp is True


def test_parse_args_defaults_inspect_sdp_to_false():
    args = main.parse_args([])
    assert args.inspect_sdp is False


def test_parse_args_defaults_sdp_mode_to_auto():
    args = main.parse_args([])
    assert args.sdp_mode == "auto"


def test_parse_args_accepts_sdp_mode_simple_and_blob():
    assert main.parse_args(["--sdp-mode", "simple"]).sdp_mode == "simple"
    assert main.parse_args(["--sdp-mode", "blob"]).sdp_mode == "blob"


def test_parse_args_rejects_unknown_sdp_mode():
    with pytest.raises(SystemExit):
        main.parse_args(["--sdp-mode", "bogus"])


def test_parse_args_rejects_unknown_transport():
    with pytest.raises(SystemExit):
        main.parse_args(["--transport", "carrier-pigeon"])


def test_parse_args_serial_without_com_produces_clean_cli_error(capsys):
    # Should be a clean argparse error (SystemExit + usage message on
    # stderr), not an unhandled ValueError traceback escaping run()/main().
    with pytest.raises(SystemExit):
        main.parse_args(["--transport", "serial"])
    captured = capsys.readouterr()
    assert "--com" in captured.err
    assert "Traceback" not in captured.err


def test_build_transport_tcp_returns_tcp_port():
    port = main.build_transport("tcp", 9100, None)
    assert isinstance(port, TcpPort)


def test_build_transport_rfcomm_returns_rfcomm_port():
    port = main.build_transport("rfcomm", 9100, None)
    assert isinstance(port, RfcommPort)


def test_build_transport_rfcomm_defaults_sdp_mode_to_auto():
    port = main.build_transport("rfcomm", 9100, None)
    assert port._sdp_mode == "auto"


def test_build_transport_rfcomm_forwards_explicit_sdp_mode():
    port = main.build_transport("rfcomm", 9100, None, sdp_mode="simple")
    assert port._sdp_mode == "simple"


def test_build_transport_serial_returns_serial_port_when_com_given():
    port = main.build_transport("serial", 9100, "COM5")
    assert isinstance(port, SerialPort)


def test_build_transport_serial_without_com_raises_value_error():
    with pytest.raises(ValueError):
        main.build_transport("serial", 9100, None)


def test_build_transport_unsupported_name_raises_value_error():
    with pytest.raises(ValueError):
        main.build_transport("carrier-pigeon", 9100, None)


def test_run_doctor_prints_a_line_per_check(monkeypatch, capsys):
    import transport.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "check_windows_service", lambda name, runner=None: CheckResult("PASS", f"{name} ok"))
    monkeypatch.setattr(diagnostics, "check_rfcomm_socket_bind", lambda: CheckResult("PASS", "bound"))
    monkeypatch.setattr(diagnostics, "check_sdp_registration", lambda: CheckResult("FAIL", "boom"))
    monkeypatch.setattr(diagnostics, "check_discoverability", lambda: CheckResult("UNKNOWN", "manual check needed"))
    monkeypatch.setattr(diagnostics, "list_paired_bluetooth_devices", lambda runner=None: CheckResult("PASS", "My Phone"))
    monkeypatch.setattr(diagnostics, "list_bluetooth_com_ports", lambda runner=None: CheckResult("PASS", "COM5"))

    main.run_doctor()

    output = capsys.readouterr().out
    assert "[PASS]" in output
    assert "[FAIL]" in output
    assert "[UNKNOWN]" in output
    assert "boom" in output
    assert "My Phone" in output


def test_run_with_doctor_flag_does_not_build_a_transport_or_viewer(monkeypatch):
    called = {"doctor": False, "build_transport": False}

    monkeypatch.setattr(main, "run_doctor", lambda: called.__setitem__("doctor", True))

    def fail_build_transport(*args, **kwargs):
        called["build_transport"] = True
        raise AssertionError("build_transport should not be called with --doctor")

    monkeypatch.setattr(main, "build_transport", fail_build_transport)

    main.run(["--doctor"])

    assert called["doctor"] is True
    assert called["build_transport"] is False


def test_run_with_inspect_sdp_flag_does_not_build_a_transport_or_viewer(monkeypatch):
    called = {"inspect_sdp": False, "build_transport": False}

    monkeypatch.setattr(main, "run_inspect_sdp", lambda: called.__setitem__("inspect_sdp", True))

    def fail_build_transport(*args, **kwargs):
        called["build_transport"] = True
        raise AssertionError("build_transport should not be called with --inspect-sdp")

    monkeypatch.setattr(main, "build_transport", fail_build_transport)

    main.run(["--inspect-sdp"])

    assert called["inspect_sdp"] is True
    assert called["build_transport"] is False


def test_run_inspect_sdp_delegates_to_sdp_inspector_module(monkeypatch):
    import transport.sdp_inspector as sdp_inspector

    called = {"ran": False}
    monkeypatch.setattr(sdp_inspector, "run_inspect_sdp", lambda: called.__setitem__("ran", True))

    main.run_inspect_sdp()

    assert called["ran"] is True


# -- _transport_title / _transport_endpoint_label -------------------------
#
# _transport_title() used to be called before the transport ever started,
# so for rfcomm it could only print a hardcoded "channel auto-assigned"
# string -- never the real channel Windows assigns. These tests pin down
# both the "not started yet" and "channel known" cases so that claim can
# never silently return.


def test_transport_title_tcp_is_unaffected_by_actual_channel_param():
    args = main.parse_args(["--transport", "tcp", "--port", "9100"])
    assert main._transport_title(args) == "TCP :9100"
    assert main._transport_title(args, actual_channel=4) == "TCP :9100"


def test_transport_title_tcp_prefers_actual_port_when_ephemeral_port_requested():
    # --port 0 asks the OS for an ephemeral port; the title must show the
    # real bound port, not the literal "0" the user passed in.
    args = main.parse_args(["--transport", "tcp", "--port", "0"])
    assert main._transport_title(args) == "TCP :0"
    assert main._transport_title(args, actual_port=64000) == "TCP :64000"


def test_transport_endpoint_label_tcp_prefers_actual_port_when_ephemeral_port_requested():
    args = main.parse_args(["--transport", "tcp", "--port", "0"])
    assert main._transport_endpoint_label(args, actual_port=64000) == "port 64000"


def test_transport_title_serial_is_unaffected_by_actual_channel_param():
    args = main.parse_args(["--transport", "serial", "--com", "COM5"])
    assert main._transport_title(args) == "Serial COM5"


def test_transport_title_rfcomm_without_channel_does_not_claim_auto_assigned():
    args = main.parse_args(["--transport", "rfcomm"])
    title = main._transport_title(args)
    assert "auto-assigned" not in title


def test_transport_title_rfcomm_with_actual_channel_shows_the_real_channel():
    args = main.parse_args(["--transport", "rfcomm"])
    title = main._transport_title(args, actual_channel=4)
    assert "4" in title
    assert "auto-assigned" not in title


def test_transport_endpoint_label_tcp():
    args = main.parse_args(["--transport", "tcp", "--port", "9100"])
    assert main._transport_endpoint_label(args) == "port 9100"


def test_transport_endpoint_label_serial():
    args = main.parse_args(["--transport", "serial", "--com", "COM5"])
    assert main._transport_endpoint_label(args) == "COM5"


def test_transport_endpoint_label_rfcomm_before_channel_is_known():
    args = main.parse_args(["--transport", "rfcomm"])
    label = main._transport_endpoint_label(args)
    assert "auto-assigned" not in label


def test_transport_endpoint_label_rfcomm_with_actual_channel():
    args = main.parse_args(["--transport", "rfcomm"])
    label = main._transport_endpoint_label(args, actual_channel=4)
    assert label == "channel 4"


# -- _make_connection_event_handler / _StatusStore --------------------------
#
# main.py used to bridge the rfcomm/tcp adapters' "client connected"/
# "client disconnected" log text into status events via a logging.Handler
# (_ConnectionEventBridge), which silently stopped working whenever the
# logger's effective level was raised above INFO. It has been replaced by
# `Port.set_connection_listener()` (see transport/port.py):
# `_make_connection_event_handler()` builds the callback main.py wires
# into that hook, translating transport-agnostic connection events into
# `TransportStatus` transitions -- independent of logging entirely.


class _FakeViewer:
    """Stands in for render.viewer.Viewer for _StatusStore tests: no Tk
    involved, `call_soon` just runs the callback immediately since there
    is no real mainloop to hop onto."""

    def __init__(self) -> None:
        self.statuses: list = []

    def call_soon(self, callback, *args) -> None:
        callback(*args)

    def update_status(self, status) -> None:
        self.statuses.append(status)


def _make_initial_status():
    from render.viewer import TransportStatus

    return TransportStatus(
        transport="tcp",
        endpoint="port 9100",
        width_mm=58,
        codepages_label="all",
    )


def test_connection_event_handler_listening_updates_status_store():
    viewer = _FakeViewer()
    status_store = main._StatusStore(_make_initial_status(), viewer)
    on_event = main._make_connection_event_handler(status_store)

    on_event("listening")

    assert viewer.statuses[-1].connection_state == "listening"


def test_connection_event_handler_connected_records_peer():
    viewer = _FakeViewer()
    status_store = main._StatusStore(_make_initial_status(), viewer)
    on_event = main._make_connection_event_handler(status_store)

    on_event("connected", peer="00:11:22:33:44:55")

    assert viewer.statuses[-1].connection_state == "connected"
    assert viewer.statuses[-1].peer == "00:11:22:33:44:55"


def test_connection_event_handler_disconnected_clears_state():
    viewer = _FakeViewer()
    status_store = main._StatusStore(_make_initial_status(), viewer)
    on_event = main._make_connection_event_handler(status_store)

    on_event("connected", peer="00:11:22:33:44:55")
    on_event("disconnected")

    assert viewer.statuses[-1].connection_state == "disconnected"
    assert viewer.statuses[-1].peer is None


def test_connection_event_handler_stopped_updates_status_store():
    viewer = _FakeViewer()
    status_store = main._StatusStore(_make_initial_status(), viewer)
    on_event = main._make_connection_event_handler(status_store)

    on_event("stopped")

    assert viewer.statuses[-1].connection_state == "stopped"


def test_connection_event_handler_data_flowing_updates_status_store():
    # "data_flowing" is the serial transport's honest stand-in for
    # "connected" -- see transport/serialport.py -- and must reach the
    # status store exactly like every other connection event.
    viewer = _FakeViewer()
    status_store = main._StatusStore(_make_initial_status(), viewer)
    on_event = main._make_connection_event_handler(status_store)

    on_event("listening")
    on_event("data_flowing")

    assert viewer.statuses[-1].connection_state == "data_flowing"


def test_no_reference_to_connection_event_bridge_remains():
    # The defect this change fixes: application state must never depend
    # on log message text. The bridge class must be gone entirely.
    assert not hasattr(main, "_ConnectionEventBridge")


# -- regression: status updates independent of --log-level ------------------
#
# This is the concrete bug the design fix addresses: TcpPort.start() used
# to only announce "client connected" via a logger.info() call, which a
# logging.Handler-based bridge silently missed once the logger's level
# was raised to WARNING or higher -- the status bar would then report
# "listening" forever while a client was actually connected. Wiring a
# real TcpPort's `set_connection_listener()` straight into
# `_make_connection_event_handler()` (exactly as main.run() does) proves
# the status store still reaches "connected", independent of log level.


# -- --dump-bytes / --replay CLI wiring -------------------------------------


def test_parse_args_defaults_dump_bytes_to_none():
    args = main.parse_args([])
    assert args.dump_bytes is None


def test_parse_args_accepts_dump_bytes_path():
    args = main.parse_args(["--dump-bytes", "capture.bin"])
    assert args.dump_bytes == "capture.bin"


def test_parse_args_defaults_replay_to_none():
    args = main.parse_args([])
    assert args.replay is None


def test_parse_args_accepts_replay_path():
    args = main.parse_args(["--replay", "capture.bin"])
    assert args.replay == "capture.bin"


def test_run_with_replay_flag_does_not_build_a_transport(monkeypatch, tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"HELLO\x0a")

    called = {"build_transport": False}

    def fail_build_transport(*args, **kwargs):
        called["build_transport"] = True
        raise AssertionError("build_transport should not be called with --replay")

    monkeypatch.setattr(main, "build_transport", fail_build_transport)

    class _FakeReplayViewer:
        def __init__(self, *a, **kw):
            self.ops = []
            self.status = kw.get("status")

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            self.ops.extend(ops)

        def add_diagnostics(self, diagnostics):
            pass

        def call_soon(self, cb, *a):
            cb(*a)

        def run(self):
            pass

        def set_title(self, *a, **k):
            pass

        def set_open_handler(self, *a, **k):
            pass

        def set_clear_handler(self, *a, **k):
            pass

    monkeypatch.setattr(main, "Viewer", _FakeReplayViewer)

    main.run(["--replay", str(capture)])

    assert called["build_transport"] is False


def test_run_with_replay_flag_shows_replay_status_not_listening(monkeypatch, tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"HELLO\x0a")

    captured = {}

    class _FakeReplayViewer:
        def __init__(self, *a, **kw):
            captured["status"] = kw.get("status")
            self.ops = []

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            self.ops.extend(ops)

        def add_diagnostics(self, diagnostics):
            pass

        def call_soon(self, cb, *a):
            cb(*a)

        def run(self):
            pass

        def set_title(self, *a, **k):
            pass

        def set_open_handler(self, *a, **k):
            pass

        def set_clear_handler(self, *a, **k):
            pass

    monkeypatch.setattr(main, "Viewer", _FakeReplayViewer)

    main.run(["--replay", str(capture)])

    status = captured["status"]
    assert status.replay_path == str(capture)


def test_run_with_replay_flag_forwards_diagnostics_to_the_viewer(monkeypatch, tmp_path):
    # Defect 1: --replay must not silently discard the parser's
    # diagnostics -- see main.replay_file()/run_replay().
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"OK" + bytes([0x00]) + b"MORE\x0a")

    captured = {"diagnostics": None}

    class _FakeReplayViewer:
        def __init__(self, *a, **kw):
            self.ops = []

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            self.ops.extend(ops)

        def add_diagnostics(self, diagnostics):
            captured["diagnostics"] = list(diagnostics)

        def call_soon(self, cb, *a):
            cb(*a)

        def run(self):
            pass

        def set_title(self, *a, **k):
            pass

        def set_open_handler(self, *a, **k):
            pass

        def set_clear_handler(self, *a, **k):
            pass

    monkeypatch.setattr(main, "Viewer", _FakeReplayViewer)

    main.run(["--replay", str(capture)])

    assert captured["diagnostics"] is not None
    assert len(captured["diagnostics"]) == 1
    assert captured["diagnostics"][0].raw_bytes == bytes([0x00])


def test_make_open_handler_forwards_diagnostics_tagged_with_the_opened_files_source(monkeypatch, tmp_path):
    # Defect 1, live-mode Open button: diagnostics from an opened file
    # must be attributable to that file, not silently merged into
    # whatever the live transport already reported (see
    # core.diagnostics.Diagnostic.source).
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"OK" + bytes([0x00]) + b"MORE\x0a")

    calls = {"cleared": False, "ops": None, "diagnostics": None}

    class _FakeViewer:
        def clear(self):
            calls["cleared"] = True

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            calls["ops"] = list(ops)

        def add_diagnostics(self, diagnostics):
            calls["diagnostics"] = list(diagnostics)

        def call_soon(self, cb, *a):
            cb(*a)

    viewer = _FakeViewer()
    on_open = main._make_open_handler(viewer, implemented_codepages=None)

    on_open(str(capture))

    assert calls["cleared"] is True
    assert calls["ops"] is not None
    assert calls["diagnostics"] is not None
    assert len(calls["diagnostics"]) == 1
    assert "capture.bin" in calls["diagnostics"][0].source


def test_make_open_handler_does_not_call_add_diagnostics_for_a_clean_file(tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"HELLO\x0a")

    calls = {"add_diagnostics_called": False}

    class _FakeViewer:
        def clear(self):
            pass

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            pass

        def add_diagnostics(self, diagnostics):
            calls["add_diagnostics_called"] = True

        def call_soon(self, cb, *a):
            cb(*a)

    viewer = _FakeViewer()
    on_open = main._make_open_handler(viewer, implemented_codepages=None)

    on_open(str(capture))

    assert calls["add_diagnostics_called"] is False


# -- Defect 1: replay/Open status bar must report real bytes/ops -----------


def test_run_replay_status_matches_file_size_and_op_count(monkeypatch, tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"HELLO\x0a")  # 6 bytes -> TextOp + LineFeedOp = 2 ops

    captured = {}

    class _FakeReplayViewer:
        def __init__(self, *a, **kw):
            self.ops = []
            self.status = kw.get("status")
            captured["viewer"] = self

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            self.ops.extend(ops)

        def add_diagnostics(self, diagnostics):
            pass

        def call_soon(self, cb, *a):
            cb(*a)

        def run(self):
            pass

        def set_title(self, *a, **k):
            pass

        def set_open_handler(self, *a, **k):
            pass

        def set_clear_handler(self, *a, **k):
            pass

    monkeypatch.setattr(main, "Viewer", _FakeReplayViewer)

    main.run(["--replay", str(capture)])

    viewer = captured["viewer"]
    expected_bytes = capture.stat().st_size
    assert expected_bytes == 6
    assert len(viewer.ops) == 2
    assert viewer.status.bytes_received == expected_bytes
    assert viewer.status.ops_count == 2


def test_run_replay_status_and_history_panel_agree_on_the_same_input(monkeypatch, tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"HELLO WORLD\x0a")

    captured = {}

    class _FakeReplayViewer:
        def __init__(self, *a, **kw):
            self.ops = []
            self.status = kw.get("status")
            captured["viewer"] = self

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            self.ops.extend(ops)
            captured["chunk_bytes"] = chunk_bytes
            captured["ops_len"] = len(ops)

        def add_diagnostics(self, diagnostics):
            pass

        def call_soon(self, cb, *a):
            cb(*a)

        def run(self):
            pass

        def set_title(self, *a, **k):
            pass

        def set_open_handler(self, *a, **k):
            pass

        def set_clear_handler(self, *a, **k):
            pass

    monkeypatch.setattr(main, "Viewer", _FakeReplayViewer)

    main.run(["--replay", str(capture)])

    viewer = captured["viewer"]
    # Invariant: the status bar counters and the history panel input must
    # be computed from the exact same byte count/op list for the same
    # replay input.
    assert viewer.status.bytes_received == captured["chunk_bytes"]
    assert viewer.status.ops_count == captured["ops_len"]


def test_make_open_handler_updates_status_accumulating_bytes_and_ops(tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"HELLO\x0a")  # 6 bytes -> 2 ops

    from render.viewer import TransportStatus

    initial_status = TransportStatus(
        transport="tcp",
        endpoint="port 9100",
        width_mm=58,
        codepages_label="all",
        bytes_received=100,
        ops_count=5,
    )

    calls = {"update_status": None}

    class _FakeViewer:
        def __init__(self):
            self._status = initial_status

        def clear(self):
            pass

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            pass

        def add_diagnostics(self, diagnostics):
            pass

        def call_soon(self, cb, *a):
            cb(*a)

        def get_status(self):
            return self._status

        def update_status(self, status):
            self._status = status
            calls["update_status"] = status

    viewer = _FakeViewer()
    on_open = main._make_open_handler(viewer, implemented_codepages=None)

    on_open(str(capture))

    assert calls["update_status"] is not None
    assert calls["update_status"].bytes_received == 100 + 6
    assert calls["update_status"].ops_count == 5 + 2


def test_run_replay_open_button_accumulates_on_top_of_initial_replay_status(monkeypatch, tmp_path):
    capture = tmp_path / "capture.bin"
    capture.write_bytes(b"HELLO\x0a")  # 6 bytes -> 2 ops

    opened = tmp_path / "opened.bin"
    opened.write_bytes(b"WORLD!!\x0a")  # 8 bytes -> 2 ops

    captured = {}

    class _FakeReplayViewer:
        def __init__(self, *a, **kw):
            self.ops = []
            self.status = kw.get("status")
            self.open_handler = None
            captured["viewer"] = self

        def update_ops(self, ops, chunk_bytes=0, byte_offsets=None):
            self.ops.extend(ops)

        def add_diagnostics(self, diagnostics):
            pass

        def call_soon(self, cb, *a):
            cb(*a)

        def run(self):
            pass

        def set_title(self, *a, **k):
            pass

        def set_open_handler(self, handler):
            self.open_handler = handler

        def set_clear_handler(self, *a, **k):
            pass

        def clear(self):
            pass

        def get_status(self):
            return self.status

        def update_status(self, status):
            self.status = status

    monkeypatch.setattr(main, "Viewer", _FakeReplayViewer)

    main.run(["--replay", str(capture)])

    viewer = captured["viewer"]
    status_before_open = viewer.status
    assert status_before_open.bytes_received == 6
    assert status_before_open.ops_count == 2

    viewer.open_handler(str(opened))

    assert viewer.status.bytes_received == 6 + 8
    assert viewer.status.ops_count == 2 + 2


def test_open_during_live_session_does_not_desync_status_store(monkeypatch, tmp_path):
    # Regression: in live mode, _StatusStore (main.py's _StatusStore) is
    # the single source of truth for the status bar -- every live chunk
    # updates it via status_store.update(). _make_open_handler's on_open
    # used to bypass it entirely (reading/writing viewer.get_status()/
    # update_status() directly), so the NEXT live chunk transformed
    # _StatusStore's stale internal snapshot -- which never learned about
    # the counts Open added -- silently discarding them and making the
    # displayed counters jump backwards.
    opened = tmp_path / "opened.bin"
    opened.write_bytes(b"BBBBBB\x0a")  # 7 bytes -> TextOp + LineFeedOp = 2 ops

    class _FakePort:
        def __init__(self):
            self.on_data = None

        def set_connection_listener(self, listener):
            pass

        def start(self, on_data):
            self.on_data = on_data

        def stop(self):
            pass

    fake_port = _FakePort()
    monkeypatch.setattr(main, "build_transport", lambda *a, **k: fake_port)

    captured = {}

    class _FakeLiveViewer:
        def __init__(self, *a, **kw):
            self.status = kw.get("status")
            self.open_handler = None
            captured["viewer"] = self

        def call_soon(self, cb, *a):
            cb(*a)

        def update_ops(self, *a, **k):
            pass

        def add_diagnostics(self, *a, **k):
            pass

        def clear(self):
            pass

        def set_title(self, *a, **k):
            pass

        def set_clear_handler(self, *a, **k):
            pass

        def set_open_handler(self, handler):
            self.open_handler = handler

        def get_status(self):
            return self.status

        def update_status(self, status):
            self.status = status

        def run(self):
            pass

    monkeypatch.setattr(main, "Viewer", _FakeLiveViewer)

    main.run(["--transport", "tcp", "--port", "9100"])

    viewer = captured["viewer"]

    # Live chunk 1: 5 bytes -> TextOp + LineFeedOp = 2 ops.
    fake_port.on_data(b"AAAA\x0a")
    assert viewer.status.bytes_received == 5
    assert viewer.status.ops_count == 2

    # Open button loads a captured file mid-session: counts must
    # accumulate on top of the live counters, not replace them.
    viewer.open_handler(str(opened))
    assert viewer.status.bytes_received == 5 + 7
    assert viewer.status.ops_count == 2 + 2

    # Live chunk 2, after Open: 10 bytes -> TextOp + LineFeedOp = 2 ops.
    fake_port.on_data(b"CCCCCCCCC\x0a")
    assert viewer.status.bytes_received == 5 + 7 + 10
    assert viewer.status.ops_count == 2 + 2 + 2


def test_status_updates_correctly_via_real_tcp_port_when_log_level_is_warning():
    import logging
    import threading

    from transport.tcp import TcpPort

    tcp_logger = logging.getLogger("btprinter.transport.tcp")
    previous_level = tcp_logger.level
    tcp_logger.setLevel(logging.WARNING)

    viewer = _FakeViewer()
    status_store = main._StatusStore(_make_initial_status(), viewer)
    on_event = main._make_connection_event_handler(status_store)

    got_connected = threading.Event()
    original_update = status_store.update

    def spying_update(transform):
        original_update(transform)
        if viewer.statuses[-1].connection_state == "connected":
            got_connected.set()

    status_store.update = spying_update

    port = TcpPort(host="127.0.0.1", port=0)
    port.set_connection_listener(on_event)

    try:
        port.start(lambda chunk: None)
        client = socket.create_connection(("127.0.0.1", port.actual_port), timeout=2.0)
        try:
            assert got_connected.wait(timeout=2.0)
        finally:
            client.close()
    finally:
        port.stop()
        tcp_logger.setLevel(previous_level)

    assert viewer.statuses[-1].connection_state in ("connected", "disconnected", "stopped")
    connected_statuses = [s for s in viewer.statuses if s.connection_state == "connected"]
    assert connected_statuses, "status store never reached the 'connected' state"
