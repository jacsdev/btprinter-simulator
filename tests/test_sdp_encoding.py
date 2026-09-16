"""Tests for transport.sdp_encoding: hand-built SDP Data Element
encoding/decoding, and the BLOB/BTH_SET_SERVICE ctypes struct layouts
used by the --sdp-mode blob registration path.

Every byte sequence asserted here was hand-derived from the Bluetooth
SDP binary spec's Data Element encoding rules (see the module docstring
in transport/sdp_encoding.py) -- these are real, checkable assertions,
not smoke tests.
"""

from __future__ import annotations

import ctypes

from transport.sdp_encoding import (
    BLOB,
    BTH_SET_SERVICE,
    ATTR_PROTOCOL_DESCRIPTOR_LIST,
    ATTR_SERVICE_CLASS_ID_LIST,
    build_browse_group_list,
    build_protocol_descriptor_list,
    build_service_class_id_list,
    build_spp_sdp_record,
    encode_attribute,
    encode_sequence,
    encode_text_string,
    encode_uint8,
    encode_uint16,
    encode_uuid16,
    find_protocol_descriptor_rfcomm_channel,
    hex_dump,
    parse_top_level_attributes,
    read_data_element,
)


# ---------------------------------------------------------------------------
# Primitive element encoding -- exact bytes
# ---------------------------------------------------------------------------


def test_encode_uint8_header_and_value():
    assert encode_uint8(4) == bytes([0x08, 0x04])


def test_encode_uint8_two_digit_channel_is_a_single_raw_byte_not_ascii():
    # Channel 12 is still a ONE-byte Unsigned Integer element (0x0C), not
    # two ASCII digit bytes -- this is exactly the single-byte-length bug
    # the task calls out: a naive/text-based encoder would produce a
    # 2-byte payload for "12" and desynchronize the surrounding sequence
    # length.
    assert encode_uint8(12) == bytes([0x08, 0x0C])


def test_encode_uint8_rejects_out_of_range_value():
    import pytest

    with pytest.raises(ValueError):
        encode_uint8(256)


def test_encode_uint16_header_and_big_endian_value():
    assert encode_uint16(0x0001) == bytes([0x09, 0x00, 0x01])
    assert encode_uint16(0x1234) == bytes([0x09, 0x12, 0x34])


def test_encode_uuid16_header_and_big_endian_value():
    assert encode_uuid16(0x1101) == bytes([0x19, 0x11, 0x01])
    assert encode_uuid16(0x0100) == bytes([0x19, 0x01, 0x00])
    assert encode_uuid16(0x0003) == bytes([0x19, 0x00, 0x03])


def test_encode_text_string_short_form_length():
    assert encode_text_string("Test") == bytes([0x25, 0x04]) + b"Test"


def test_encode_sequence_short_form_length():
    assert encode_sequence(bytes([0x19, 0x11, 0x01])) == bytes([0x35, 0x03, 0x19, 0x11, 0x01])


def test_encode_sequence_two_byte_length_form_when_payload_exceeds_255_bytes():
    payload = bytes(300)
    encoded = encode_sequence(payload)
    assert encoded[0] == 0x36  # (SEQUENCE << 3) | size_descriptor 6
    assert encoded[1:3] == (300).to_bytes(2, "big")
    assert encoded[3:] == payload


def test_encode_attribute_prefixes_value_with_uint16_attribute_id():
    value = encode_uuid16(0x1101)
    encoded = encode_attribute(ATTR_SERVICE_CLASS_ID_LIST, value)
    assert encoded == bytes([0x09, 0x00, 0x01]) + value


# ---------------------------------------------------------------------------
# Composite attribute builders -- exact bytes
# ---------------------------------------------------------------------------


def test_build_service_class_id_list_exact_bytes():
    assert build_service_class_id_list() == bytes([0x35, 0x03, 0x19, 0x11, 0x01])


def test_build_browse_group_list_exact_bytes():
    assert build_browse_group_list() == bytes([0x35, 0x03, 0x19, 0x10, 0x02])


def test_build_protocol_descriptor_list_channel_4_exact_bytes():
    expected = bytes(
        [
            0x35, 0x0C,  # outer sequence, 12 bytes
            0x35, 0x03, 0x19, 0x01, 0x00,  # inner sequence: L2CAP UUID
            0x35, 0x05, 0x19, 0x00, 0x03, 0x08, 0x04,  # inner sequence: RFCOMM UUID, channel 4
        ]
    )
    assert build_protocol_descriptor_list(4) == expected


def test_build_protocol_descriptor_list_channel_12_exact_bytes_same_length_as_channel_4():
    expected = bytes(
        [
            0x35, 0x0C,
            0x35, 0x03, 0x19, 0x01, 0x00,
            0x35, 0x05, 0x19, 0x00, 0x03, 0x08, 0x0C,  # only the channel byte differs
        ]
    )
    encoded = build_protocol_descriptor_list(12)
    assert encoded == expected
    # Regression guard: byte length must be identical to channel 4's
    # encoding -- a text/BCD-based channel encoder would change length
    # for a two-digit channel and break the outer sequence's length byte.
    assert len(encoded) == len(build_protocol_descriptor_list(4))


def test_build_spp_sdp_record_contains_all_four_attributes_in_order():
    record = build_spp_sdp_record(4, "Test")
    element_type, sequence_body, next_offset = read_data_element(record, 0)
    assert element_type == 6  # TYPE_SEQUENCE
    assert next_offset == len(record)

    attributes = parse_top_level_attributes(record)
    attribute_ids = [attribute_id for attribute_id, _value_type, _value_bytes in attributes]
    assert attribute_ids == [0x0001, 0x0004, 0x0005, 0x0100]


# ---------------------------------------------------------------------------
# Decoding -- read_data_element / parse_top_level_attributes /
# find_protocol_descriptor_rfcomm_channel
# ---------------------------------------------------------------------------


def test_read_data_element_decodes_fixed_size_uuid16():
    element_type, value, next_offset = read_data_element(bytes([0x19, 0x11, 0x01]), 0)
    assert element_type == 3  # TYPE_UUID
    assert value == bytes([0x11, 0x01])
    assert next_offset == 3


def test_read_data_element_decodes_short_form_sequence():
    element_type, value, next_offset = read_data_element(bytes([0x35, 0x02, 0xAA, 0xBB]), 0)
    assert element_type == 6  # TYPE_SEQUENCE
    assert value == bytes([0xAA, 0xBB])
    assert next_offset == 4


def test_read_data_element_raises_on_truncated_input():
    import pytest

    with pytest.raises(ValueError):
        read_data_element(bytes([0x19, 0x11]), 0)  # UUID16 needs 2 more bytes


def test_find_protocol_descriptor_rfcomm_channel_round_trips_channel_4():
    record = build_spp_sdp_record(4, "58mm Thermal Printer Simulator")
    found, channel = find_protocol_descriptor_rfcomm_channel(record)
    assert found is True
    assert channel == 4


def test_find_protocol_descriptor_rfcomm_channel_round_trips_channel_12():
    record = build_spp_sdp_record(12, "58mm Thermal Printer Simulator")
    found, channel = find_protocol_descriptor_rfcomm_channel(record)
    assert found is True
    assert channel == 12


def test_find_protocol_descriptor_rfcomm_channel_reports_absent_when_attribute_missing():
    # A record with only a ServiceClassIDList -- no ProtocolDescriptorList
    # at all, simulating the minimal/malformed record hypothesis this
    # task investigates.
    record = encode_sequence(encode_attribute(ATTR_SERVICE_CLASS_ID_LIST, build_service_class_id_list()))
    found, channel = find_protocol_descriptor_rfcomm_channel(record)
    assert found is False
    assert channel is None


def test_find_protocol_descriptor_rfcomm_channel_never_raises_on_garbage_bytes():
    found, channel = find_protocol_descriptor_rfcomm_channel(b"\xff\xff\xff")
    assert found is False
    assert channel is None


def test_find_protocol_descriptor_rfcomm_channel_reports_present_but_no_channel_when_list_is_empty_sequence():
    record = encode_sequence(encode_attribute(ATTR_PROTOCOL_DESCRIPTOR_LIST, encode_sequence(b"")))
    found, channel = find_protocol_descriptor_rfcomm_channel(record)
    assert found is True
    assert channel is None


# ---------------------------------------------------------------------------
# hex_dump formatting
# ---------------------------------------------------------------------------


def test_hex_dump_formats_offset_hex_and_ascii_columns():
    dump = hex_dump(b"HELLO")
    assert "0000" in dump
    assert "48 45 4c 4c 4f" in dump
    assert "HELLO" in dump


def test_hex_dump_empty_input():
    assert hex_dump(b"") == "(empty)"


def test_hex_dump_wraps_at_bytes_per_line():
    dump = hex_dump(bytes(range(20)), bytes_per_line=16)
    lines = dump.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("0000")
    assert lines[1].startswith("0010")


# ---------------------------------------------------------------------------
# Win32 struct layouts -- ctypes.sizeof()/offsets checked against the
# documented BTH_SET_SERVICE (learn.microsoft.com/windows/win32/api/
# ws2bth/ns-ws2bth-bth_set_service) and winsock2.h BLOB layouts.
# ---------------------------------------------------------------------------


def test_blob_struct_size_is_16_bytes_on_64_bit():
    # ULONG cbSize (4 bytes) + 4 bytes padding to align the following
    # pointer + BYTE *pBlobData (8 bytes) = 16 bytes on a 64-bit process.
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "this project targets 64-bit Windows Python only"
    assert ctypes.sizeof(BLOB) == 16


def test_bth_set_service_struct_field_offsets_match_documented_layout():
    assert BTH_SET_SERVICE.pSdpVersion.offset == 0
    assert BTH_SET_SERVICE.pRecordHandle.offset == 8
    assert BTH_SET_SERVICE.fCodService.offset == 16
    assert BTH_SET_SERVICE.Reserved.offset == 20
    assert BTH_SET_SERVICE.ulRecordLength.offset == 40
    assert BTH_SET_SERVICE.pRecord.offset == 44


def test_bth_set_service_struct_size_is_48_bytes_on_64_bit():
    # pSdpVersion(8) + pRecordHandle(8) + fCodService(4) + Reserved[5](20)
    # + ulRecordLength(4) + pRecord[1](1) = 45 bytes, padded to 48 for
    # 8-byte pointer alignment.
    assert ctypes.sizeof(BTH_SET_SERVICE) == 48
