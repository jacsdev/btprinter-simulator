"""Hand-built Bluetooth SDP (Service Discovery Protocol) record encoding
and decoding.

Windows' `WSASetServiceW` can synthesize a minimal SDP record on its own
from a plain `WSAQUERYSETW` (see `transport/rfcomm.py`'s
`register_sdp_service`), but that synthesized record may omit a proper
`ProtocolDescriptorList` (the attribute a remote SDP client actually
needs to learn the RFCOMM channel to connect to). This module builds a
complete record by hand, byte-for-byte, per the Bluetooth Core
Specification's SDP binary encoding (Vol 3, Part B, Section 3: "Service
Attribute Protocol" / "Data Element" encoding), so it can be handed to
`WSASetServiceW` via `WSAQUERYSETW.lpBlob` (see
`register_sdp_service_blob` in `transport/rfcomm.py`), and so
`--inspect-sdp` can decode whatever raw SDP bytes Windows reports back
for a locally registered service.

Data Element encoding (SDP binary spec): each element starts with one
header byte, `(type_descriptor << 3) | size_descriptor`:

- Type descriptor (high 5 bits): 0 Nil, 1 Unsigned Integer, 2 Signed
  Integer, 3 UUID, 4 Text String, 5 Boolean, 6 Data Element Sequence,
  7 Data Element Alternative, 8 URL.
- Size descriptor (low 3 bits): for fixed-size types (UInt/SInt/UUID),
  0..4 select a 1/2/4/8/16-byte value with no explicit length field.
  For variable-length types (Text String, Sequence, Alternative, URL),
  5/6/7 select an explicit 1/2/4-byte big-endian length field
  immediately after the header byte, followed by that many bytes of
  payload.

All multi-byte integers in an SDP record are big-endian, per the spec.
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Data Element type descriptors (high 5 bits of the header byte)
# ---------------------------------------------------------------------------

TYPE_NIL = 0
TYPE_UNSIGNED_INT = 1
TYPE_SIGNED_INT = 2
TYPE_UUID = 3
TYPE_TEXT_STRING = 4
TYPE_BOOLEAN = 5
TYPE_SEQUENCE = 6
TYPE_ALTERNATIVE = 7
TYPE_URL = 8

# ---------------------------------------------------------------------------
# Bluetooth SIG-assigned UUIDs/protocol numbers used by this record.
#
# UUID_L2CAP and UUID_RFCOMM intentionally have the same numeric values
# as Windows' own BTHPROTO_L2CAP/BTHPROTO_RFCOMM constants (ws2bth.h) --
# that is not a coincidence, Windows chose those constants to match the
# Bluetooth SIG protocol identifiers used in SDP records.
# ---------------------------------------------------------------------------

UUID_SERIAL_PORT = 0x1101  # Serial Port Profile (SPP)
UUID_L2CAP = 0x0100
UUID_RFCOMM = 0x0003
UUID_PUBLIC_BROWSE_ROOT = 0x1002

# Universal SDP attribute IDs (Bluetooth Core Spec, Vol 3, Part B, 5.1).
ATTR_SERVICE_CLASS_ID_LIST = 0x0001
ATTR_PROTOCOL_DESCRIPTOR_LIST = 0x0004
ATTR_BROWSE_GROUP_LIST = 0x0005
# ServiceName under the primary language's LanguageBaseAttributeIDList
# offset (0x0100). LanguageBaseAttributeIDList (attribute 0x0006) itself
# is omitted here: this is a deliberate simplification (single-language,
# English/UTF-8 record) matching the minimum record shape Windows'
# own SDP client and Android's SDP client both accept in practice.
ATTR_SERVICE_NAME = 0x0100


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_uint8(value: int) -> bytes:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"uint8 value out of range: {value}")
    return bytes([(TYPE_UNSIGNED_INT << 3) | 0, value])


def encode_uint16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"uint16 value out of range: {value}")
    return bytes([(TYPE_UNSIGNED_INT << 3) | 1]) + struct.pack(">H", value)


def encode_uuid16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"uuid16 value out of range: {value}")
    return bytes([(TYPE_UUID << 3) | 1]) + struct.pack(">H", value)


def encode_text_string(value: str) -> bytes:
    payload = value.encode("utf-8")
    return _encode_variable_length(TYPE_TEXT_STRING, payload)


def encode_sequence(elements: bytes) -> bytes:
    return _encode_variable_length(TYPE_SEQUENCE, elements)


def _encode_variable_length(type_descriptor: int, payload: bytes) -> bytes:
    length = len(payload)
    if length <= 0xFF:
        header = bytes([(type_descriptor << 3) | 5, length])
    elif length <= 0xFFFF:
        header = bytes([(type_descriptor << 3) | 6]) + struct.pack(">H", length)
    else:
        header = bytes([(type_descriptor << 3) | 7]) + struct.pack(">I", length)
    return header + payload


def encode_attribute(attribute_id: int, value: bytes) -> bytes:
    """An SDP attribute is an (attribute ID, attribute value) pair: the ID
    is always a 16-bit Unsigned Integer element, immediately followed by
    the value element."""
    return encode_uint16(attribute_id) + value


def build_service_class_id_list() -> bytes:
    return encode_sequence(encode_uuid16(UUID_SERIAL_PORT))


def build_protocol_descriptor_list(channel: int) -> bytes:
    """ProtocolDescriptorList = Sequence[ Sequence[L2CAP UUID],
    Sequence[RFCOMM UUID, channel] ] -- the shape a remote SDP client
    needs to learn both the protocol stack and the RFCOMM channel to
    connect to."""
    l2cap_descriptor = encode_sequence(encode_uuid16(UUID_L2CAP))
    rfcomm_descriptor = encode_sequence(encode_uuid16(UUID_RFCOMM) + encode_uint8(channel))
    return encode_sequence(l2cap_descriptor + rfcomm_descriptor)


def build_browse_group_list() -> bytes:
    return encode_sequence(encode_uuid16(UUID_PUBLIC_BROWSE_ROOT))


def build_spp_sdp_record(channel: int, service_name: str) -> bytes:
    """Build the complete raw SDP record for the Serial Port Profile
    service on `channel`, as a single top-level Data Element Sequence of
    (attribute ID, attribute value) pairs -- exactly the byte stream
    `WSAQUERYSETW.lpBlob` needs to point to via `BTH_SET_SERVICE.pRecord`
    (see `register_sdp_service_blob` in transport/rfcomm.py)."""
    attributes = (
        encode_attribute(ATTR_SERVICE_CLASS_ID_LIST, build_service_class_id_list())
        + encode_attribute(ATTR_PROTOCOL_DESCRIPTOR_LIST, build_protocol_descriptor_list(channel))
        + encode_attribute(ATTR_BROWSE_GROUP_LIST, build_browse_group_list())
        + encode_attribute(ATTR_SERVICE_NAME, encode_text_string(service_name))
    )
    return encode_sequence(attributes)


# ---------------------------------------------------------------------------
# Decoding -- used by transport/sdp_inspector.py to interpret whatever raw
# SDP bytes WSALookupServiceNextW reports back for a local record. Never
# raises out to callers: any element this simplified parser cannot make
# sense of is reported as "not found", never fabricated.
# ---------------------------------------------------------------------------


def read_data_element(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Decode one Data Element starting at `data[offset]`. Returns
    `(type_descriptor, value_bytes, next_offset)`. Raises `ValueError` on
    truncated/malformed input -- callers that parse untrusted/unknown
    blobs must catch that."""
    if offset >= len(data):
        raise ValueError("data element header out of bounds")
    header = data[offset]
    type_descriptor = header >> 3
    size_descriptor = header & 0x07
    offset += 1
    if type_descriptor == TYPE_NIL:
        return type_descriptor, b"", offset
    if size_descriptor <= 4:
        length = 1 << size_descriptor
        value = data[offset : offset + length]
        if len(value) != length:
            raise ValueError("truncated fixed-size data element")
        return type_descriptor, value, offset + length
    if size_descriptor == 5:
        if offset >= len(data):
            raise ValueError("truncated 1-byte length field")
        length = data[offset]
        offset += 1
    elif size_descriptor == 6:
        if offset + 2 > len(data):
            raise ValueError("truncated 2-byte length field")
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2
    elif size_descriptor == 7:
        if offset + 4 > len(data):
            raise ValueError("truncated 4-byte length field")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
    else:
        raise ValueError(f"reserved size descriptor: {size_descriptor}")
    value = data[offset : offset + length]
    if len(value) != length:
        raise ValueError("truncated variable-length data element")
    return type_descriptor, value, offset + length


def parse_top_level_attributes(record_bytes: bytes) -> list[tuple[int, int, bytes]]:
    """Parse a full SDP record (a single top-level Sequence of attribute
    ID/value pairs) into `[(attribute_id, value_type, value_bytes), ...]`.
    Raises `ValueError` if `record_bytes` does not start with a Sequence
    or is malformed."""
    element_type, sequence_body, _ = read_data_element(record_bytes, 0)
    if element_type != TYPE_SEQUENCE:
        raise ValueError("SDP record does not start with a Data Element Sequence")
    pairs: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset < len(sequence_body):
        id_type, id_value, offset = read_data_element(sequence_body, offset)
        if id_type != TYPE_UNSIGNED_INT:
            raise ValueError("expected an attribute ID (Unsigned Integer) element")
        attribute_id = int.from_bytes(id_value, "big")
        value_type, value_bytes, offset = read_data_element(sequence_body, offset)
        pairs.append((attribute_id, value_type, value_bytes))
    return pairs


def find_protocol_descriptor_rfcomm_channel(record_bytes: bytes) -> tuple[bool, Optional[int]]:
    """Best-effort inspection of a raw SDP record: returns
    `(protocol_descriptor_list_present, rfcomm_channel_or_None)`.

    Never raises: any blob this simplified decoder cannot parse is
    reported as `(False, None)` -- `--inspect-sdp` must never fabricate a
    channel number it did not actually decode.
    """
    try:
        attributes = parse_top_level_attributes(record_bytes)
    except (ValueError, IndexError):
        return False, None
    for attribute_id, value_type, value_bytes in attributes:
        if attribute_id != ATTR_PROTOCOL_DESCRIPTOR_LIST:
            continue
        if value_type != TYPE_SEQUENCE:
            return True, None
        try:
            channel = _extract_rfcomm_channel(value_bytes)
        except (ValueError, IndexError):
            channel = None
        return True, channel
    return False, None


def _extract_rfcomm_channel(protocol_descriptor_list_body: bytes) -> Optional[int]:
    offset = 0
    while offset < len(protocol_descriptor_list_body):
        element_type, descriptor_bytes, offset = read_data_element(protocol_descriptor_list_body, offset)
        if element_type != TYPE_SEQUENCE:
            continue
        inner_offset = 0
        saw_rfcomm_uuid = False
        while inner_offset < len(descriptor_bytes):
            inner_type, inner_value, inner_offset = read_data_element(descriptor_bytes, inner_offset)
            if inner_type == TYPE_UUID and len(inner_value) == 2 and int.from_bytes(inner_value, "big") == UUID_RFCOMM:
                saw_rfcomm_uuid = True
            elif saw_rfcomm_uuid and inner_type == TYPE_UNSIGNED_INT:
                return int.from_bytes(inner_value, "big")
    return None


def hex_dump(data: bytes, bytes_per_line: int = 16) -> str:
    """Classic offset/hex/ASCII hex dump, used by --inspect-sdp to show
    the raw SDP attribute bytes it could not otherwise decode."""
    lines = []
    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset : offset + bytes_per_line]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset:04x}  {hex_part:<{bytes_per_line * 3}}  {ascii_part}")
    return "\n".join(lines) if lines else "(empty)"


# ---------------------------------------------------------------------------
# Win32 structs for the lpBlob registration path (ws2bth.h's BTH_SET_SERVICE,
# and winsock2.h's BLOB). Confidence: high -- transcribed verbatim from
# Microsoft Learn's published BTH_SET_SERVICE syntax
# (learn.microsoft.com/windows/win32/api/ws2bth/ns-ws2bth-bth_set_service)
# and the standard winsock2.h BLOB struct. sizeof()/offsets are asserted in
# tests/test_sdp_encoding.py against the documented field layout.
# ---------------------------------------------------------------------------


class BLOB(ctypes.Structure):
    """winsock2.h: `typedef struct _BLOB { ULONG cbSize; BYTE *pBlobData; } BLOB, *LPBLOB;`"""

    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("pBlobData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class BTH_SET_SERVICE(ctypes.Structure):
    """ws2bth.h: passed via `WSAQUERYSETW.lpBlob` (through a `BLOB`) to
    register/delete a hand-built SDP record. `pRecord` is a flexible
    array (`UCHAR pRecord[1]` in the C header) -- callers allocate a raw
    buffer of `sizeof(BTH_SET_SERVICE) - 1 + record_length` bytes and
    copy the real SDP record bytes in starting at `pRecord`'s offset
    (see `register_sdp_service_blob` in transport/rfcomm.py)."""

    _fields_ = [
        ("pSdpVersion", ctypes.POINTER(ctypes.c_ulong)),
        ("pRecordHandle", ctypes.POINTER(ctypes.c_void_p)),
        ("fCodService", ctypes.c_ulong),
        ("Reserved", ctypes.c_ulong * 5),
        ("ulRecordLength", ctypes.c_ulong),
        ("pRecord", ctypes.c_ubyte * 1),
    ]


BTH_SDP_VERSION = 1  # ws2bth.h: #define BTH_SDP_VERSION 1


@dataclass(frozen=True)
class DecodedSdpRecord:
    """Result of inspecting one raw SDP record's attribute bytes --
    used by transport/sdp_inspector.py's formatting/verdict logic."""

    protocol_descriptor_list_present: bool
    rfcomm_channel: Optional[int]
