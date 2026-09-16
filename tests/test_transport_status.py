"""Tests for the pure status model in render.viewer: TransportStatus's
state transitions and the two formatting functions that turn a status
into displayed text (status bar line and empty-state message).

None of these touch Tkinter -- they exercise plain dataclasses and
string-formatting functions, per the TDD note in the task: Tk widgets
are awkward to test, so the pure parts get real assertions instead.
"""

from __future__ import annotations

from render.viewer import (
    TransportStatus,
    format_empty_state_message,
    format_status_line,
)


def _status(**overrides) -> TransportStatus:
    defaults = dict(
        transport="rfcomm",
        endpoint="channel (assigning...)",
        width_mm=58,
        codepages_label="all",
        sdp_uuid=None,
    )
    defaults.update(overrides)
    return TransportStatus(**defaults)


# -- state transitions -------------------------------------------------


def test_status_starts_in_starting_state():
    status = _status()
    assert status.connection_state == "starting"


def test_listening_transition_sets_connection_state():
    status = _status().listening()
    assert status.connection_state == "listening"


def test_client_connected_transition_records_peer():
    status = _status().listening().client_connected(peer="00:11:22:33:44:55")
    assert status.connection_state == "connected"
    assert status.peer == "00:11:22:33:44:55"


def test_client_connected_transition_allows_unknown_peer():
    status = _status().listening().client_connected(peer=None)
    assert status.connection_state == "connected"
    assert status.peer is None


def test_client_disconnected_transition_clears_peer():
    status = _status().listening().client_connected(peer="00:11:22:33:44:55").client_disconnected()
    assert status.connection_state == "disconnected"
    assert status.peer is None


def test_stopped_transition_sets_connection_state():
    status = _status().listening().stopped()
    assert status.connection_state == "stopped"


def test_data_flowing_transition_sets_connection_state():
    status = _status(transport="serial").listening().data_flowing()
    assert status.connection_state == "data_flowing"


def test_add_bytes_accumulates_across_calls():
    status = _status().add_bytes(10).add_bytes(5)
    assert status.bytes_received == 15


def test_add_ops_accumulates_across_calls():
    status = _status().add_ops(2).add_ops(3)
    assert status.ops_count == 5


def test_with_endpoint_updates_channel_without_touching_other_fields():
    status = _status(connection_state="listening").with_endpoint("channel 4")
    assert status.endpoint == "channel 4"
    assert status.connection_state == "listening"


def test_with_endpoint_can_also_set_sdp_uuid():
    status = _status().with_endpoint("channel 4", sdp_uuid="00001101-0000-1000-8000-00805F9B34FB")
    assert status.endpoint == "channel 4"
    assert status.sdp_uuid == "00001101-0000-1000-8000-00805F9B34FB"


def test_transitions_return_new_instances_not_mutate_in_place():
    original = _status()
    updated = original.listening()
    assert original.connection_state == "starting"
    assert updated.connection_state == "listening"


def test_reset_counters_zeroes_bytes_and_ops_but_keeps_everything_else():
    status = _status(transport="tcp", endpoint="port 9100").listening().add_bytes(50).add_ops(4)

    reset = status.reset_counters()

    assert reset.bytes_received == 0
    assert reset.ops_count == 0
    assert reset.transport == "tcp"
    assert reset.endpoint == "port 9100"
    assert reset.connection_state == "listening"


def test_reset_counters_returns_a_new_instance():
    status = _status().add_bytes(10)
    reset = status.reset_counters()
    assert status.bytes_received == 10
    assert reset.bytes_received == 0


# -- format_status_line --------------------------------------------------


def test_format_status_line_shows_real_channel_not_placeholder():
    status = _status(endpoint="channel 4").listening()
    line = format_status_line(status)
    assert "channel 4" in line
    assert "assigning" not in line
    assert "auto-assigned" not in line


def test_format_status_line_includes_sdp_uuid_for_rfcomm():
    status = _status(
        endpoint="channel 4",
        sdp_uuid="00001101-0000-1000-8000-00805F9B34FB",
    )
    line = format_status_line(status)
    assert "00001101-0000-1000-8000-00805F9B34FB" in line


def test_format_status_line_omits_sdp_uuid_when_none():
    status = _status(transport="tcp", endpoint="port 9100", sdp_uuid=None)
    line = format_status_line(status)
    assert "UUID" not in line


def test_format_status_line_shows_paper_width_and_codepages():
    status = _status(width_mm=80, codepages_label="0,2,16")
    line = format_status_line(status)
    assert "80" in line
    assert "0,2,16" in line


def test_format_status_line_shows_connected_peer():
    status = _status(endpoint="channel 4").listening().client_connected(peer="00:11:22:33:44:55")
    line = format_status_line(status)
    assert "connected" in line.lower()
    assert "00:11:22:33:44:55" in line


def test_format_status_line_shows_bytes_and_ops_counters():
    status = _status().add_bytes(128).add_ops(3)
    line = format_status_line(status)
    assert "128" in line
    assert "3" in line


# -- format_empty_state_message ------------------------------------------


def test_format_empty_state_message_mentions_channel_when_idle():
    status = _status(endpoint="channel 4").listening()
    message = format_empty_state_message(status)
    assert "channel 4" in message


def test_format_empty_state_message_is_unmistakably_alive():
    status = _status(endpoint="channel 4").listening()
    message = format_empty_state_message(status)
    # Should read as "running and idle", not blank/frozen.
    assert "waiting" in message.lower()


def test_format_empty_state_message_changes_once_connected():
    idle_message = format_empty_state_message(_status(endpoint="channel 4").listening())
    connected_message = format_empty_state_message(
        _status(endpoint="channel 4").listening().client_connected(peer="00:11:22:33:44:55")
    )
    assert idle_message != connected_message
    assert "connected" in connected_message.lower()


def test_format_empty_state_message_handles_missing_status():
    message = format_empty_state_message(None)
    assert "waiting" in message.lower()


# -- replay mode (--replay / Open button) --------------------------------


def test_format_status_line_for_replay_mode_shows_file_and_never_claims_listening():
    status = _status(replay_path="C:/captures/sample.bin")
    line = format_status_line(status)
    assert "sample.bin" in line
    assert "listening" not in line.lower()


def test_format_status_line_for_live_mode_is_unaffected_by_replay_path_default():
    status = _status().listening()
    line = format_status_line(status)
    assert "listening" in line.lower()


# -- serial transport: honest connection status (no fake accept boundary) --
#
# A Windows incoming Bluetooth COM port has no accept/disconnect boundary
# this transport can observe (see transport/serialport.py). These tests
# pin down the invariant from the bug report: the displayed status text
# must never claim to be "listening for a client" while bytes are
# arriving, must never say "disconnected" (unobservable for this
# transport), and must never contradict the byte/op counters shown
# beside it.


def test_serial_listening_label_does_not_claim_a_client_connection():
    status = _status(transport="serial", endpoint="COM3").listening()
    line = format_status_line(status)
    assert "listening for a client" not in line.lower()
    assert "client" not in line.lower()


def test_serial_data_flowing_label_does_not_say_listening_for_a_client():
    status = _status(transport="serial", endpoint="COM3").listening().data_flowing()
    line = format_status_line(status)
    assert "listening for a client" not in line.lower()
    assert "receiving" in line.lower()


def test_serial_status_and_counters_never_contradict_while_receiving():
    status = (
        _status(transport="serial", endpoint="COM3")
        .listening()
        .data_flowing()
        .add_bytes(108)
        .add_ops(12)
    )
    line = format_status_line(status)
    assert "listening for a client" not in line.lower()
    assert "108" in line
    assert "12" in line


def test_serial_status_returns_to_waiting_after_idle_without_claiming_disconnected():
    status = (
        _status(transport="serial", endpoint="COM3")
        .listening()
        .data_flowing()
        .add_bytes(108)
        .add_ops(12)
        .listening()  # idle timeout: back to the waiting state
    )
    line = format_status_line(status)
    assert "disconnected" not in line.lower()
    assert "listening for a client" not in line.lower()
    assert "108" in line
    assert "12" in line


def test_tcp_listening_label_is_unchanged_by_the_serial_specific_wording():
    status = _status(transport="tcp", endpoint="port 9100").listening()
    line = format_status_line(status)
    assert "listening for a client" in line.lower()


def test_format_empty_state_message_for_replay_mode_never_claims_listening():
    status = _status(replay_path="C:/captures/sample.bin")
    message = format_empty_state_message(status)
    assert "sample.bin" in message
    assert "listening" not in message.lower()
