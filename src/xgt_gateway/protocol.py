"""LS ELECTRIC XGT dedicated-protocol frame codec.

The implementation follows the supplied XGT manual:
  - 20-byte LSIS header
  - 0x0054/0x0055 continuous read request/response
  - 0x0058/0x0059 continuous write request/response
  - 0x0014 continuous BYTE data type
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .addressing import normalize_continuous_byte_address
from .errors import XgtPlcError, XgtProtocolError

HEADER_SIZE = 20
COMPANY_ID = b"LSIS-XGT\x00\x00"
COMPANY_ID_GLOFA = b"LGIS-GLOFA"
SOURCE_CLIENT = 0x33
SOURCE_SERVER = 0x11

CMD_READ_REQUEST = 0x0054
CMD_READ_RESPONSE = 0x0055
CMD_WRITE_REQUEST = 0x0058
CMD_WRITE_RESPONSE = 0x0059
DATA_TYPE_CONTINUOUS = 0x0014

ERROR_MESSAGES = {
    0x0001: "Individual read/write block count exceeded 16",
    0x0002: "Unsupported data type",
    0x0003: "Unsupported PLC device area",
    0x0004: "PLC device address range exceeded",
    0x0005: "Block size exceeded 1400 bytes",
    0x0006: "Total block size exceeded 1400 bytes",
    0x0075: "Invalid XGT company header",
    0x0076: "Invalid XGT length field",
    0x0077: "Invalid XGT checksum",
    0x0078: "Invalid XGT command",
}


@dataclass(frozen=True)
class XgtHeader:
    company_id: bytes
    plc_info: int
    cpu_info: int
    source: int
    invoke_id: int
    length: int
    position: int
    bcc: int


@dataclass(frozen=True)
class XgtFrame:
    header: XgtHeader
    payload: bytes


def build_header(
    payload_length: int,
    invoke_id: int,
    *,
    source: int = SOURCE_CLIENT,
    plc_info: int = 0,
    cpu_info: int = 0xA0,
    slot: int = 0,
    base: int = 0,
    use_bcc: bool = True,
    company_id: bytes = COMPANY_ID,
) -> bytes:
    if len(company_id) != 10:
        raise ValueError("XGT company ID must be exactly 10 bytes")
    if not 0 <= payload_length <= 0xFFFF:
        raise ValueError("XGT payload length is outside the 16-bit field")
    position = ((base & 0x0F) << 4) | (slot & 0x0F)
    first_19 = (
        company_id
        + struct.pack("<H", plc_info & 0xFFFF)
        + bytes((cpu_info & 0xFF, source & 0xFF))
        + struct.pack("<HH", invoke_id & 0xFFFF, payload_length)
        + bytes((position,))
    )
    bcc = sum(first_19) & 0xFF if use_bcc else 0
    return first_19 + bytes((bcc,))


def parse_header(raw: bytes, *, validate_bcc: bool = False) -> XgtHeader:
    if len(raw) != HEADER_SIZE:
        raise XgtProtocolError(f"XGT header must be {HEADER_SIZE} bytes, got {len(raw)}")
    company_id = raw[:10]
    if company_id not in {COMPANY_ID, COMPANY_ID_GLOFA}:
        raise XgtProtocolError(f"Unexpected XGT company ID: {company_id!r}")
    plc_info = struct.unpack_from("<H", raw, 10)[0]
    cpu_info = raw[12]
    source = raw[13]
    invoke_id, length = struct.unpack_from("<HH", raw, 14)
    position, bcc = raw[18], raw[19]
    if validate_bcc and bcc not in {0, sum(raw[:19]) & 0xFF}:
        raise XgtProtocolError(
            f"Invalid XGT BCC: received 0x{bcc:02X}, expected 0x{sum(raw[:19]) & 0xFF:02X}"
        )
    return XgtHeader(company_id, plc_info, cpu_info, source, invoke_id, length, position, bcc)


def build_continuous_read_request(
    address: str,
    byte_count: int,
    invoke_id: int,
    *,
    cpu_info: int = 0xA0,
    slot: int = 0,
    base: int = 0,
    use_bcc: bool = True,
) -> bytes:
    if not 1 <= byte_count <= 1400:
        raise ValueError("Continuous XGT read length must be 1..1400 bytes")
    encoded_address = normalize_continuous_byte_address(address).encode("ascii")
    payload = struct.pack(
        "<HHHHH", CMD_READ_REQUEST, DATA_TYPE_CONTINUOUS, 0, 1, len(encoded_address)
    ) + encoded_address + struct.pack("<H", byte_count)
    return build_header(
        len(payload), invoke_id, cpu_info=cpu_info, slot=slot, base=base, use_bcc=use_bcc
    ) + payload


def build_continuous_write_request(
    address: str,
    data: bytes,
    invoke_id: int,
    *,
    cpu_info: int = 0xA0,
    slot: int = 0,
    base: int = 0,
    use_bcc: bool = True,
) -> bytes:
    payload_data = bytes(data)
    if not 1 <= len(payload_data) <= 1400:
        raise ValueError("Continuous XGT write length must be 1..1400 bytes")
    encoded_address = normalize_continuous_byte_address(address).encode("ascii")
    payload = (
        struct.pack("<HHHHH", CMD_WRITE_REQUEST, DATA_TYPE_CONTINUOUS, 0, 1, len(encoded_address))
        + encoded_address
        + struct.pack("<H", len(payload_data))
        + payload_data
    )
    return build_header(
        len(payload), invoke_id, cpu_info=cpu_info, slot=slot, base=base, use_bcc=use_bcc
    ) + payload


def split_frame(frame: bytes, *, validate_bcc: bool = False) -> XgtFrame:
    if len(frame) < HEADER_SIZE:
        raise XgtProtocolError("Truncated XGT frame")
    header = parse_header(frame[:HEADER_SIZE], validate_bcc=validate_bcc)
    if len(frame) != HEADER_SIZE + header.length:
        raise XgtProtocolError(
            f"XGT frame length mismatch: header says {header.length}, received {len(frame) - HEADER_SIZE}"
        )
    return XgtFrame(header, frame[HEADER_SIZE:])


def _validate_response(frame: XgtFrame, expected_command: int, invoke_id: int) -> bytes:
    if frame.header.source != SOURCE_SERVER:
        raise XgtProtocolError(f"Unexpected response source 0x{frame.header.source:02X}")
    if frame.header.invoke_id != (invoke_id & 0xFFFF):
        raise XgtProtocolError(
            f"Invoke ID mismatch: expected {invoke_id & 0xFFFF}, got {frame.header.invoke_id}"
        )
    payload = frame.payload
    if len(payload) < 10:
        raise XgtProtocolError("XGT response payload is too short")
    command, data_type, _reserved, error_status = struct.unpack_from("<HHHH", payload, 0)
    if command != expected_command:
        raise XgtProtocolError(
            f"Unexpected XGT response command 0x{command:04X}; expected 0x{expected_command:04X}"
        )
    if data_type != DATA_TYPE_CONTINUOUS:
        raise XgtProtocolError(f"Unexpected XGT response data type 0x{data_type:04X}")
    if error_status != 0:
        error_code = struct.unpack_from("<H", payload, 8)[0]
        detail = ERROR_MESSAGES.get(error_code, "Unknown PLC error")
        raise XgtPlcError(error_code, f"PLC XGT error 0x{error_code:04X}: {detail}")
    return payload


def parse_continuous_read_response(frame: XgtFrame, invoke_id: int, expected_count: int) -> bytes:
    payload = _validate_response(frame, CMD_READ_RESPONSE, invoke_id)
    if len(payload) < 12:
        raise XgtProtocolError("XGT continuous-read ACK is too short")
    block_count, data_length = struct.unpack_from("<HH", payload, 8)
    if block_count != 1:
        raise XgtProtocolError(f"Continuous-read ACK block count must be 1, got {block_count}")
    data = payload[12:]
    if data_length != len(data):
        raise XgtProtocolError(
            f"Continuous-read data length mismatch: field={data_length}, actual={len(data)}"
        )
    if data_length != expected_count:
        raise XgtProtocolError(
            f"Continuous-read returned {data_length} bytes; expected {expected_count}"
        )
    return data


def parse_continuous_write_response(frame: XgtFrame, invoke_id: int) -> None:
    payload = _validate_response(frame, CMD_WRITE_RESPONSE, invoke_id)
    block_count = struct.unpack_from("<H", payload, 8)[0]
    if block_count != 1:
        raise XgtProtocolError(f"Continuous-write ACK block count must be 1, got {block_count}")

