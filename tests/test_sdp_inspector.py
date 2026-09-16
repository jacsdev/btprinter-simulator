"""Tests for transport.sdp_inspector (`python main.py --inspect-sdp`).

The ctypes/WSALookupService* boundary is fully mocked here -- these tests
feed in fake WSAQUERYSETW results (built the same way real Windows would
populate them) and assert the resulting SdpRecordInfo/format_report
output, never making a real Bluetooth/Winsock call. See
tests/test_rfcomm.py's module docstring for why real calls are out of
scope for unit tests.
"""

from __future__ import annotations

import ctypes

from transport.rfcomm import AF_BLUETOOTH, CSADDR_INFO, SOCKADDR_BTH, WSAQUERYSETW, make_spp_guid
from transport.sdp_encoding import BLOB, build_spp_sdp_record
from transport.sdp_inspector import (
    WSA_E_NO_MORE,
    SdpInspectionResult,
    SdpRecordInfo,
    detect_simulator_running_hint,
    enumerate_local_sdp_records,
    format_report,
)


# ---------------------------------------------------------------------------
# Fake ws2_32 boundary
# ---------------------------------------------------------------------------


class _FakeWs2_32BeginFails:
    def WSALookupServiceBeginW(self, restrictions_ptr, flags, handle_ptr) -> int:
        return -1

    def WSAGetLastError(self) -> int:
        return 10093  # WSANOTINITIALISED -- arbitrary but realistic

    def WSALookupServiceEnd(self, handle) -> int:
        return 0


class _FakeWs2_32NoRecordsEnumerated:
    """Simulates the documented Windows quirk: WSALookupServiceBeginW
    succeeds, but the very first WSALookupServiceNextW call already
    reports WSA_E_NO_MORE -- zero records enumerated."""

    def WSALookupServiceBeginW(self, restrictions_ptr, flags, handle_ptr) -> int:
        return 0

    def WSALookupServiceNextW(self, handle, flags, buffer_len_ptr, buffer) -> int:
        return -1

    def WSAGetLastError(self) -> int:
        return WSA_E_NO_MORE

    def WSALookupServiceEnd(self, handle) -> int:
        return 0


class _FakeWs2_32OneRecord:
    """Simulates one enumerated record with a real SOCKADDR_BTH and a
    real hand-built SDP record blob (built via
    transport.sdp_encoding.build_spp_sdp_record), the way a genuine
    Windows response would be laid out in memory."""

    def __init__(self, service_name: str, channel: int, record_bytes: bytes) -> None:
        self._service_name = service_name
        self._channel = channel
        self._record_bytes = record_bytes
        self._next_call_count = 0
        self._kept_alive: list = []  # prevents premature GC of nested structs

    def WSALookupServiceBeginW(self, restrictions_ptr, flags, handle_ptr) -> int:
        return 0

    def WSALookupServiceNextW(self, handle, flags, buffer_len_ptr, buffer) -> int:
        self._next_call_count += 1
        if self._next_call_count > 1:
            return -1

        query_set = ctypes.cast(buffer, ctypes.POINTER(WSAQUERYSETW)).contents
        ctypes.memset(ctypes.byref(query_set), 0, ctypes.sizeof(query_set))
        query_set.dwSize = ctypes.sizeof(WSAQUERYSETW)
        query_set.lpszServiceInstanceName = self._service_name

        guid = make_spp_guid()
        self._kept_alive.append(guid)
        query_set.lpServiceClassId = ctypes.pointer(guid)

        sockaddr = SOCKADDR_BTH(AF_BLUETOOTH, 0, guid, self._channel)
        self._kept_alive.append(sockaddr)
        csaddr = CSADDR_INFO()
        ctypes.memset(ctypes.byref(csaddr), 0, ctypes.sizeof(csaddr))
        csaddr.LocalAddr.lpSockaddr = ctypes.pointer(sockaddr)
        csaddr.LocalAddr.iSockaddrLength = ctypes.sizeof(SOCKADDR_BTH)
        self._kept_alive.append(csaddr)
        query_set.dwNumberOfCsAddrs = 1
        query_set.lpcsaBuffer = ctypes.pointer(csaddr)

        blob_buffer = ctypes.create_string_buffer(self._record_bytes, len(self._record_bytes))
        self._kept_alive.append(blob_buffer)
        blob = BLOB()
        blob.cbSize = len(self._record_bytes)
        blob.pBlobData = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
        self._kept_alive.append(blob)
        query_set.lpBlob = ctypes.cast(ctypes.byref(blob), ctypes.c_void_p)

        return 0

    def WSAGetLastError(self) -> int:
        return WSA_E_NO_MORE

    def WSALookupServiceEnd(self, handle) -> int:
        return 0


class _FakeWs2_32HollowPlaceholderRecord:
    """Simulates the real, measured behavior of this development
    machine's BTH namespace provider: WSALookupServiceBeginW succeeds
    (with LUP_CONTAINERS in the flags, required just to avoid WSAEINVAL
    here), and WSALookupServiceNextW returns exactly one entry with no
    service instance name, no service class GUID, no CSADDR_INFO, and no
    lpBlob -- a hollow placeholder carrying no actual service data."""

    def __init__(self) -> None:
        self._next_call_count = 0

    def WSALookupServiceBeginW(self, restrictions_ptr, flags, handle_ptr) -> int:
        return 0

    def WSALookupServiceNextW(self, handle, flags, buffer_len_ptr, buffer) -> int:
        self._next_call_count += 1
        if self._next_call_count > 1:
            return -1
        query_set = ctypes.cast(buffer, ctypes.POINTER(WSAQUERYSETW)).contents
        ctypes.memset(ctypes.byref(query_set), 0, ctypes.sizeof(query_set))
        query_set.dwSize = ctypes.sizeof(WSAQUERYSETW)
        return 0

    def WSAGetLastError(self) -> int:
        return WSA_E_NO_MORE

    def WSALookupServiceEnd(self, handle) -> int:
        return 0


class _FakeWs2_32NextFailsWithUnexpectedError:
    def WSALookupServiceBeginW(self, restrictions_ptr, flags, handle_ptr) -> int:
        return 0

    def WSALookupServiceNextW(self, handle, flags, buffer_len_ptr, buffer) -> int:
        return -1

    def WSAGetLastError(self) -> int:
        return 10004  # WSAEINTR -- an unexpected, non-"no more results" error

    def WSALookupServiceEnd(self, handle) -> int:
        return 0


# ---------------------------------------------------------------------------
# enumerate_local_sdp_records -- limitation cases
# ---------------------------------------------------------------------------


def test_enumerate_reports_limitation_when_begin_fails():
    result = enumerate_local_sdp_records(ws2_32=_FakeWs2_32BeginFails())
    assert result.records == []
    assert result.limitation is not None
    assert "WSALookupServiceBeginW" in result.limitation
    assert "10093" in result.limitation


def test_enumerate_reports_limitation_when_zero_records_enumerated():
    result = enumerate_local_sdp_records(ws2_32=_FakeWs2_32NoRecordsEnumerated())
    assert result.records == []
    assert result.limitation is not None
    assert "LUP_RES_SERVICE" in result.limitation
    assert "does NOT prove" in result.limitation


def test_enumerate_reports_limitation_when_only_hollow_placeholder_entries_found():
    # Confirmed by real, on-machine testing: this is exactly what this
    # development machine's BTH provider returns even while a real SDP
    # service is actively registered via WSASetServiceW -- see the final
    # report's "real-machine verification" section.
    result = enumerate_local_sdp_records(ws2_32=_FakeWs2_32HollowPlaceholderRecord())
    assert result.records != []  # the hollow entry IS returned...
    assert result.limitation is not None  # ...but treated as unusable
    assert "hollow placeholder" in result.limitation


def test_format_report_shows_raw_hollow_entries_and_still_reports_limitation():
    result = enumerate_local_sdp_records(ws2_32=_FakeWs2_32HollowPlaceholderRecord())
    report = format_report(result)
    assert "Raw entries enumerated" in report
    assert "LIMITATION:" in report
    assert "VERDICT: UNKNOWN" in report


def test_enumerate_reports_limitation_on_unexpected_next_error():
    result = enumerate_local_sdp_records(ws2_32=_FakeWs2_32NextFailsWithUnexpectedError())
    assert result.records == []
    assert result.limitation is not None
    assert "10004" in result.limitation


# ---------------------------------------------------------------------------
# enumerate_local_sdp_records -- successful enumeration
# ---------------------------------------------------------------------------


def test_enumerate_decodes_service_name_and_channel_for_a_complete_record():
    record_bytes = build_spp_sdp_record(4, "58mm Thermal Printer Simulator")
    fake = _FakeWs2_32OneRecord("58mm Thermal Printer Simulator", 4, record_bytes)
    result = enumerate_local_sdp_records(ws2_32=fake)

    assert result.limitation is None
    assert len(result.records) == 1
    record = result.records[0]
    assert record.service_instance_name == "58mm Thermal Printer Simulator"
    assert record.port == 4
    assert record.protocol_descriptor_list_present is True
    assert record.rfcomm_channel == 4
    assert record.service_class_guid == "00001101-0000-1000-8000-00805f9b34fb"


def test_enumerate_reports_missing_protocol_descriptor_list_when_record_lacks_one():
    from transport.sdp_encoding import (
        ATTR_SERVICE_CLASS_ID_LIST,
        build_service_class_id_list,
        encode_attribute,
        encode_sequence,
    )

    minimal_record = encode_sequence(
        encode_attribute(ATTR_SERVICE_CLASS_ID_LIST, build_service_class_id_list())
    )
    fake = _FakeWs2_32OneRecord("Minimal Service", 4, minimal_record)
    result = enumerate_local_sdp_records(ws2_32=fake)

    assert result.limitation is None
    record = result.records[0]
    assert record.protocol_descriptor_list_present is False
    assert record.rfcomm_channel is None


# ---------------------------------------------------------------------------
# detect_simulator_running_hint
# ---------------------------------------------------------------------------


def test_detect_simulator_running_hint_confirms_when_default_service_name_found():
    records = [
        SdpRecordInfo(
            service_instance_name="58mm Thermal Printer Simulator",
            service_class_guid=None,
            address_family=None,
            bt_addr=None,
            port=None,
            blob_bytes=None,
            protocol_descriptor_list_present=False,
            rfcomm_channel=None,
        )
    ]
    hint = detect_simulator_running_hint(records)
    assert "CONFIRMED RUNNING" in hint


def test_detect_simulator_running_hint_is_unknown_when_no_matching_record():
    hint = detect_simulator_running_hint([])
    assert "UNKNOWN" in hint
    assert "does NOT mean" in hint


# ---------------------------------------------------------------------------
# format_report -- verdict line for both "channel found" and "not found" cases
# ---------------------------------------------------------------------------


def test_format_report_verdict_yes_when_channel_found():
    record_bytes = build_spp_sdp_record(4, "58mm Thermal Printer Simulator")
    fake = _FakeWs2_32OneRecord("58mm Thermal Printer Simulator", 4, record_bytes)
    result = enumerate_local_sdp_records(ws2_32=fake)
    report = format_report(result)

    assert "VERDICT: YES" in report
    assert "[4]" in report
    assert "CONFIRMED RUNNING" in report


def test_format_report_verdict_no_when_protocol_descriptor_list_missing():
    from transport.sdp_encoding import (
        ATTR_SERVICE_CLASS_ID_LIST,
        build_service_class_id_list,
        encode_attribute,
        encode_sequence,
    )

    minimal_record = encode_sequence(
        encode_attribute(ATTR_SERVICE_CLASS_ID_LIST, build_service_class_id_list())
    )
    fake = _FakeWs2_32OneRecord("Minimal Service", 4, minimal_record)
    result = enumerate_local_sdp_records(ws2_32=fake)
    report = format_report(result)

    assert "VERDICT: NO" in report


def test_format_report_verdict_unknown_when_nothing_enumerated():
    result = enumerate_local_sdp_records(ws2_32=_FakeWs2_32NoRecordsEnumerated())
    report = format_report(result)

    assert "VERDICT: UNKNOWN" in report
    assert "LIMITATION:" in report


def test_format_report_includes_hex_dump_of_blob_bytes():
    record_bytes = build_spp_sdp_record(4, "Test")
    fake = _FakeWs2_32OneRecord("Test", 4, record_bytes)
    result = enumerate_local_sdp_records(ws2_32=fake)
    report = format_report(result)

    assert "Raw SDP attribute blob" in report
    # first byte of any of our records is a Data Element Sequence header
    # (0x35 or 0x36) -- confirm the hex dump actually rendered real bytes.
    assert "35" in report or "36" in report
