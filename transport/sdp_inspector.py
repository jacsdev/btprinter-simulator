"""`python main.py --inspect-sdp`: enumerates what is ACTUALLY published
in the local Bluetooth SDP database via `WSALookupServiceBeginW` /
`WSALookupServiceNextW` / `WSALookupServiceEnd` (`ws2_32.dll`, `NS_BTH`
namespace), instead of trusting the log line RfcommPort.start() prints
when it registers a service.

This exists because the real, measured symptom this diagnostic
investigates is: the simulator logs a successful SDP registration, a
paired phone sees the PC, but RFCOMM connect never even reaches the
accept loop -- meaning SDP resolution itself is failing on the phone's
side, silently. `--doctor`'s `check_sdp_registration` only proves
`WSASetServiceW` returned success; it says nothing about what Windows
actually put in the local SDP database. This module answers that
question directly, by reading the database back the same way a remote
SDP client's local counterpart would.

CRITICAL, and the reason for the verdict/limitation machinery below:
Windows' `LUP_RES_SERVICE` flag does not reliably enumerate
locally-registered SDP records on every Windows build, even while a
service is actively registered via `WSASetServiceW`. When that happens,
this module reports that plainly as a limitation -- it never fabricates
a record or prints a misleading "OK" for something it could not actually
observe.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Optional

from transport import sdp_encoding
from transport.rfcomm import (
    CSADDR_INFO,
    DEFAULT_SERVICE_NAME,
    GUID,
    NS_BTH,
    SOCKADDR_BTH,
    WSAQUERYSETW,
    _ws2_32,
    _wsa_startup,
)

# winsock2.h / nspapi.h flags and error codes used by WSALookupService*.
# Values transcribed from Microsoft's published winsock2.h (see
# WebSearch citations in the accompanying report -- these are the exact
# hex constants, not guesses).
LUP_RES_SERVICE = 0x8000
LUP_RETURN_NAME = 0x0010
LUP_RETURN_TYPE = 0x0020
LUP_RETURN_VERSION = 0x0040
LUP_RETURN_COMMENT = 0x0080
LUP_RETURN_ADDR = 0x0100
LUP_RETURN_BLOB = 0x0200
LUP_RETURN_ALIASES = 0x0400
LUP_RETURN_QUERY_STRING = 0x0800
LUP_RETURN_ALL = (
    LUP_RETURN_NAME
    | LUP_RETURN_TYPE
    | LUP_RETURN_VERSION
    | LUP_RETURN_COMMENT
    | LUP_RETURN_ADDR
    | LUP_RETURN_BLOB
    | LUP_RETURN_ALIASES
    | LUP_RETURN_QUERY_STRING
)  # 0x0FF0
LUP_CONTAINERS = 0x0002

# Confirmed by real, on-machine testing (see the accompanying report):
# WSALookupServiceBeginW(dwNameSpace=NS_BTH) returns WSAEINVAL (10022) on
# this Windows build unless LUP_CONTAINERS is also set -- with or without
# LUP_RES_SERVICE, with or without LUP_RETURN_ALL. LUP_CONTAINERS is
# documented for Bluetooth as triggering a device-container enumeration,
# not literally "local services", but it is required just to make the
# call succeed at all on this build. LUP_RES_SERVICE is kept because it
# is the documented flag for restricting results to the *local* SDP
# database, per Microsoft's Bluetooth WSALookupService documentation.
BEGIN_FLAGS = LUP_RES_SERVICE | LUP_RETURN_ALL | LUP_CONTAINERS

WSA_E_NO_MORE = 10110  # winerror.h: no more results from WSALookupServiceNext
WSAENOMORE = 10102  # older/deprecated alias for the same condition
WSAEFAULT = 10014  # buffer too small; *lpdwBufferLength holds the required size

_INITIAL_BUFFER_SIZE = 4096


@dataclass(frozen=True)
class SdpRecordInfo:
    """One record read back from the local SDP database via
    WSALookupServiceNextW, plus what --inspect-sdp could decode from it."""

    service_instance_name: Optional[str]
    service_class_guid: Optional[str]
    address_family: Optional[int]
    bt_addr: Optional[int]
    port: Optional[int]
    blob_bytes: Optional[bytes]
    protocol_descriptor_list_present: bool
    rfcomm_channel: Optional[int]


@dataclass(frozen=True)
class SdpInspectionResult:
    records: list[SdpRecordInfo]
    limitation: Optional[str]
    """Human-readable explanation when enumeration could not be trusted
    (a WSALookupService* call failed, or returned zero records). `None`
    when at least one record was genuinely enumerated."""


def _guid_to_str(guid: GUID) -> str:
    data4 = bytes(bytearray(guid.Data4))
    return (
        f"{guid.Data1:08x}-{guid.Data2:04x}-{guid.Data3:04x}-"
        f"{data4[0]:02x}{data4[1]:02x}-"
        f"{data4[2]:02x}{data4[3]:02x}{data4[4]:02x}{data4[5]:02x}{data4[6]:02x}{data4[7]:02x}"
    )


def _build_lookup_restrictions() -> WSAQUERYSETW:
    query_set = WSAQUERYSETW()
    ctypes.memset(ctypes.byref(query_set), 0, ctypes.sizeof(query_set))
    query_set.dwSize = ctypes.sizeof(WSAQUERYSETW)
    query_set.dwNameSpace = NS_BTH
    return query_set


def _extract_record_info(query_set: WSAQUERYSETW) -> SdpRecordInfo:
    service_instance_name = query_set.lpszServiceInstanceName or None

    service_class_guid = None
    if query_set.lpServiceClassId:
        service_class_guid = _guid_to_str(query_set.lpServiceClassId.contents)

    address_family: Optional[int] = None
    bt_addr: Optional[int] = None
    port: Optional[int] = None
    if query_set.dwNumberOfCsAddrs and query_set.lpcsaBuffer:
        csaddr: CSADDR_INFO = query_set.lpcsaBuffer[0]
        if csaddr.LocalAddr.lpSockaddr and csaddr.LocalAddr.iSockaddrLength >= ctypes.sizeof(SOCKADDR_BTH):
            sockaddr = csaddr.LocalAddr.lpSockaddr.contents
            address_family = sockaddr.addressFamily
            bt_addr = sockaddr.btAddr
            port = sockaddr.port

    blob_bytes: Optional[bytes] = None
    protocol_descriptor_list_present = False
    rfcomm_channel: Optional[int] = None
    if query_set.lpBlob:
        blob = ctypes.cast(query_set.lpBlob, ctypes.POINTER(sdp_encoding.BLOB)).contents
        if blob.pBlobData and blob.cbSize:
            blob_bytes = ctypes.string_at(blob.pBlobData, blob.cbSize)
            protocol_descriptor_list_present, rfcomm_channel = sdp_encoding.find_protocol_descriptor_rfcomm_channel(
                blob_bytes
            )

    return SdpRecordInfo(
        service_instance_name=service_instance_name,
        service_class_guid=service_class_guid,
        address_family=address_family,
        bt_addr=bt_addr,
        port=port,
        blob_bytes=blob_bytes,
        protocol_descriptor_list_present=protocol_descriptor_list_present,
        rfcomm_channel=rfcomm_channel,
    )


def enumerate_local_sdp_records(ws2_32=None) -> SdpInspectionResult:
    """Enumerate local Bluetooth SDP records via WSALookupServiceBeginW /
    WSALookupServiceNextW / WSALookupServiceEnd. `ws2_32` is injectable so
    tests can exercise this without a real Bluetooth stack -- production
    callers never pass it (defaults to the real ws2_32.dll binding)."""
    dll = ws2_32 if ws2_32 is not None else _ws2_32
    if ws2_32 is None:
        # WSALookupServiceBeginW requires a prior WSAStartup in this
        # process -- RfcommPort.start() normally does this as a side
        # effect, but --inspect-sdp must work standalone (simulator not
        # running). Skipped for the injected-fake test path.
        _wsa_startup()
    restrictions = _build_lookup_restrictions()
    lookup_handle = ctypes.c_void_p()

    begin_result = dll.WSALookupServiceBeginW(
        ctypes.byref(restrictions), BEGIN_FLAGS, ctypes.byref(lookup_handle)
    )
    if begin_result != 0:
        error_code = dll.WSAGetLastError()
        return SdpInspectionResult(
            records=[],
            limitation=(
                f"WSALookupServiceBeginW failed with Win32 error {error_code}; "
                "the local Bluetooth SDP database could not be queried at all."
            ),
        )

    records: list[SdpRecordInfo] = []
    buffer_size = ctypes.c_ulong(_INITIAL_BUFFER_SIZE)
    result_buffer = ctypes.create_string_buffer(buffer_size.value)
    enumeration_error: Optional[str] = None
    try:
        while True:
            next_result = dll.WSALookupServiceNextW(
                lookup_handle, 0, ctypes.byref(buffer_size), result_buffer
            )
            if next_result != 0:
                error_code = dll.WSAGetLastError()
                if error_code in (WSA_E_NO_MORE, WSAENOMORE):
                    break
                if error_code == WSAEFAULT:
                    # buffer_size now holds the required size -- grow and retry.
                    result_buffer = ctypes.create_string_buffer(buffer_size.value)
                    continue
                enumeration_error = (
                    f"WSALookupServiceNextW failed with Win32 error {error_code} "
                    f"after enumerating {len(records)} record(s)."
                )
                break
            query_set = ctypes.cast(result_buffer, ctypes.POINTER(WSAQUERYSETW)).contents
            records.append(_extract_record_info(query_set))
    finally:
        dll.WSALookupServiceEnd(lookup_handle)

    if enumeration_error is not None:
        return SdpInspectionResult(records=records, limitation=enumeration_error)

    meaningful_records = [record for record in records if _carries_service_data(record)]

    if not records:
        return SdpInspectionResult(
            records=[],
            limitation=(
                "WSALookupServiceBeginW/Next enumerated ZERO local service records "
                "(WSA_E_NO_MORE on the very first call). This is a known "
                "inconsistency in Windows' SDP lookup APIs: LUP_RES_SERVICE does "
                "not reliably enumerate locally-registered SDP records on every "
                "Windows build, even while a service is actively registered via "
                "WSASetServiceW. This does NOT prove no SDP record is published "
                "-- it means this inspector could not see one on this build."
            ),
        )

    if not meaningful_records:
        return SdpInspectionResult(
            records=records,
            limitation=(
                f"WSALookupServiceBeginW/Next enumerated {len(records)} entr"
                f"{'y' if len(records) == 1 else 'ies'}, but none carried any "
                "service data (no service instance name, no service class "
                "GUID, no CSADDR_INFO/SOCKADDR_BTH, no lpBlob) -- these are "
                "hollow placeholder entries (confirmed by real, on-machine "
                "testing: this happens with LUP_CONTAINERS, the flag "
                "required just to make WSALookupServiceBeginW succeed at "
                "all on this Windows build's BTH namespace provider), NOT "
                "our actual registered SDP record. This is the same "
                "LUP_RES_SERVICE inconsistency described above: it does NOT "
                "prove no SDP record is published, only that this inspector "
                "could not see one on this build."
            ),
        )

    return SdpInspectionResult(records=records, limitation=None)


def _carries_service_data(record: SdpRecordInfo) -> bool:
    """True if `record` has any actual content -- as opposed to the
    hollow placeholder entries this Windows build's BTH namespace
    provider returns (see BEGIN_FLAGS' docstring/comment above)."""
    return bool(
        record.service_instance_name
        or record.service_class_guid
        or record.port is not None
        or record.blob_bytes
    )


def detect_simulator_running_hint(records: list[SdpRecordInfo]) -> str:
    """Best-effort, NOT authoritative: this codebase writes no PID file or
    lock --inspect-sdp could check directly to know whether an RfcommPort
    is currently running in another process.

    WSASetService-registered SDP records are ephemeral and do not persist
    after the registering process exits (Microsoft's own WSASetService
    documentation: "SDP records advertised by WSASetService do not
    persist after the process that published them has quit."). So finding
    a record named `DEFAULT_SERVICE_NAME` here IS positive evidence the
    simulator is currently running. NOT finding one is inconclusive --
    LUP_RES_SERVICE may simply have failed to enumerate anything at all
    (see the limitation machinery above) -- so this function never claims
    the simulator is stopped, only whether it could confirm it is running.
    """
    for record in records:
        if record.service_instance_name == DEFAULT_SERVICE_NAME:
            return (
                f"CONFIRMED RUNNING: found a local SDP record named "
                f"'{DEFAULT_SERVICE_NAME}' -- since WSASetService "
                "registrations do not persist after the registering process "
                "exits, this means some process currently has this simulator's "
                "RFCOMM listener open."
            )
    return (
        "UNKNOWN: no record named "
        f"'{DEFAULT_SERVICE_NAME}' was found. This does NOT mean the "
        "simulator is not running -- it may be running with a record "
        "LUP_RES_SERVICE simply failed to enumerate (see above), or it may "
        "genuinely not be running. This codebase keeps no PID file/lock this "
        "inspector could check to tell those two cases apart."
    )


def format_report(result: SdpInspectionResult) -> str:
    """Human-readable report: one section per enumerated record, the
    running-state hint, and a single unambiguous VERDICT line."""
    lines: list[str] = []
    lines.append("=== --inspect-sdp: local Bluetooth SDP database ===")
    lines.append("")

    if result.limitation is not None:
        if result.records:
            lines.append(f"Raw entries enumerated ({len(result.records)}), for transparency:")
            for index, record in enumerate(result.records, start=1):
                lines.append(
                    f"  entry {index}: name={record.service_instance_name!r} "
                    f"guid={record.service_class_guid} port={record.port} "
                    f"blob_bytes={len(record.blob_bytes) if record.blob_bytes else 0}"
                )
            lines.append("")
        lines.append("LIMITATION:")
        lines.append(result.limitation)
        lines.append("")
        lines.append(
            "VERDICT: UNKNOWN -- no local SDP record could be enumerated to "
            "inspect. This does NOT confirm the published record is broken; "
            "it means this diagnostic tool could not observe it via "
            "WSALookupService* on this Windows build."
        )
        return "\n".join(lines)

    lines.append(f"Enumerated {len(result.records)} local SDP record(s):")
    lines.append("")
    for index, record in enumerate(result.records, start=1):
        lines.append(f"--- Record {index} ---")
        lines.append(f"  Service instance name : {record.service_instance_name!r}")
        lines.append(f"  Service class GUID    : {record.service_class_guid}")
        if record.address_family is not None:
            lines.append(f"  Address family        : {record.address_family}")
            lines.append(f"  btAddr                : 0x{record.bt_addr:012x}" if record.bt_addr else "  btAddr                : 0x0")
            lines.append(f"  Port (RFCOMM channel) : {record.port}")
        else:
            lines.append("  CSADDR_INFO/SOCKADDR_BTH : not present in this record")
        if record.blob_bytes is not None:
            lines.append(f"  Raw SDP attribute blob ({len(record.blob_bytes)} bytes):")
            for dump_line in sdp_encoding.hex_dump(record.blob_bytes).splitlines():
                lines.append(f"    {dump_line}")
            lines.append(
                f"  ProtocolDescriptorList (attr 0x0004) present: {record.protocol_descriptor_list_present}"
            )
            lines.append(f"  RFCOMM channel decoded from blob: {record.rfcomm_channel}")
        else:
            lines.append("  No lpBlob returned for this record -- cannot inspect raw SDP attributes.")
        lines.append("")

    lines.append(detect_simulator_running_hint(result.records))
    lines.append("")

    has_complete_rfcomm_descriptor = any(
        record.protocol_descriptor_list_present and record.rfcomm_channel is not None
        for record in result.records
    )
    if has_complete_rfcomm_descriptor:
        matching_channels = sorted(
            {
                record.rfcomm_channel
                for record in result.records
                if record.protocol_descriptor_list_present and record.rfcomm_channel is not None
            }
        )
        lines.append(
            "VERDICT: YES -- at least one enumerated record contains a "
            f"ProtocolDescriptorList carrying an RFCOMM channel ({matching_channels}). "
            "A remote SDP client that can enumerate this record would be able "
            "to learn the RFCOMM channel from it."
        )
    else:
        any_blob = any(record.blob_bytes is not None for record in result.records)
        if any_blob:
            lines.append(
                "VERDICT: NO -- no enumerated record's SDP attribute blob "
                "contains a decodable ProtocolDescriptorList with an RFCOMM "
                "channel. A remote SDP client would NOT be able to learn the "
                "RFCOMM channel from what is currently published."
            )
        else:
            lines.append(
                "VERDICT: UNKNOWN -- record(s) were enumerated, but none "
                "returned an lpBlob this inspector could decode (Windows may "
                "be synthesizing the record's binary form without exposing it "
                "back through WSALookupServiceNextW). Cannot confirm or deny "
                "whether a ProtocolDescriptorList with an RFCOMM channel is "
                "actually published."
            )

    return "\n".join(lines)


def run_inspect_sdp(print_fn=print, ws2_32=None) -> None:
    """Entry point for `python main.py --inspect-sdp`."""
    result = enumerate_local_sdp_records(ws2_32=ws2_32)
    print_fn(format_report(result))
