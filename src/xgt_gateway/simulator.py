"""Small XGT/TCP simulator used for development and automated tests."""

from __future__ import annotations

import logging
import re
import socketserver
import struct
import threading
from typing import Any

from .protocol import (
    CMD_READ_REQUEST,
    CMD_READ_RESPONSE,
    CMD_WRITE_REQUEST,
    CMD_WRITE_RESPONSE,
    DATA_TYPE_CONTINUOUS,
    HEADER_SIZE,
    SOURCE_SERVER,
    build_header,
    parse_header,
)

LOGGER = logging.getLogger(__name__)
_ADDRESS_RE = re.compile(r"^%([A-Z])B(\d+)$")


class _SimulatorTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16


class XgtPlcSimulator:
    """In-memory server supporting the two continuous BYTE operations."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, memory_size: int = 65_536) -> None:
        self.host = host
        self.port = port
        self.memory_size = memory_size
        self._lock = threading.RLock()
        self._areas: dict[str, bytearray] = {}
        self._server: _SimulatorTcpServer | None = None
        self._thread: threading.Thread | None = None
        self.read_count = 0
        self.write_count = 0
        self._drop_next_write = False

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return self.host, self.port
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server:
            return
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                while True:
                    try:
                        raw_header = self._recv_exact(HEADER_SIZE)
                    except ConnectionError:
                        return
                    header = parse_header(raw_header)
                    payload = self._recv_exact(header.length)
                    try:
                        response = owner.handle_payload(header.invoke_id, payload)
                    except ConnectionAbortedError:
                        return
                    self.request.sendall(response)

            def _recv_exact(self, size: int) -> bytes:
                chunks: list[bytes] = []
                remaining = size
                while remaining:
                    chunk = self.request.recv(remaining)
                    if not chunk:
                        raise ConnectionError("client disconnected")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks)

        server = _SimulatorTcpServer((self.host, self.port), Handler)
        try:
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="xgt-plc-simulator",
                daemon=True,
            )
            thread.start()
        except Exception:
            server.server_close()
            raise
        self._server = server
        self._thread = thread
        LOGGER.info("XGT PLC simulator listening on %s:%s", *self.address)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def fill_incrementing(self, device: str = "D") -> None:
        area = self._area(device)
        with self._lock:
            for index in range(len(area)):
                area[index] = index & 0xFF

    def read_memory(self, device: str, offset: int, length: int) -> bytes:
        with self._lock:
            area = self._area(device)
            return bytes(area[offset : offset + length])

    def write_memory(self, device: str, offset: int, data: bytes) -> None:
        with self._lock:
            area = self._area(device)
            end = offset + len(data)
            if end > len(area):
                raise ValueError("Simulator memory range exceeded")
            area[offset:end] = data

    def drop_next_write_connection(self) -> None:
        """Close the next write request without an ACK (test fault injection)."""

        with self._lock:
            self._drop_next_write = True

    def handle_payload(self, invoke_id: int, payload: bytes) -> bytes:
        try:
            if len(payload) < 10:
                return self._error(invoke_id, CMD_READ_RESPONSE, 0x0076)
            command, data_type, _reserved, block_count, name_length = struct.unpack_from("<HHHHH", payload, 0)
            if data_type != DATA_TYPE_CONTINUOUS:
                return self._error(invoke_id, self._response_command(command), 0x0002)
            if block_count != 1:
                return self._error(invoke_id, self._response_command(command), 0x0001)
            if len(payload) < 10 + name_length + 2:
                return self._error(invoke_id, self._response_command(command), 0x0076)
            address = payload[10 : 10 + name_length].decode("ascii")
            device, offset = self._parse_address(address)
            data_length = struct.unpack_from("<H", payload, 10 + name_length)[0]
            if data_length > 1400:
                return self._error(invoke_id, self._response_command(command), 0x0005)
            if offset + data_length > self.memory_size:
                return self._error(invoke_id, self._response_command(command), 0x0004)

            if command == CMD_READ_REQUEST:
                data = self.read_memory(device, offset, data_length)
                with self._lock:
                    self.read_count += 1
                body = struct.pack(
                    "<HHHHHH", CMD_READ_RESPONSE, DATA_TYPE_CONTINUOUS, 0, 0, 1, len(data)
                ) + data
            elif command == CMD_WRITE_REQUEST:
                data_offset = 12 + name_length
                data = payload[data_offset : data_offset + data_length]
                if len(data) != data_length:
                    return self._error(invoke_id, CMD_WRITE_RESPONSE, 0x0076)
                with self._lock:
                    if self._drop_next_write:
                        self._drop_next_write = False
                        raise ConnectionAbortedError("simulated connection loss before write ACK")
                self.write_memory(device, offset, data)
                with self._lock:
                    self.write_count += 1
                body = struct.pack("<HHHHH", CMD_WRITE_RESPONSE, DATA_TYPE_CONTINUOUS, 0, 0, 1)
            else:
                return self._error(invoke_id, self._response_command(command), 0x0078)
            return build_header(
                len(body),
                invoke_id,
                source=SOURCE_SERVER,
                plc_info=0x0101,
                cpu_info=0xA0,
                use_bcc=True,
            ) + body
        except (UnicodeDecodeError, ValueError):
            return self._error(invoke_id, CMD_READ_RESPONSE, 0x0003)

    def _error(self, invoke_id: int, response_command: int, error_code: int) -> bytes:
        body = struct.pack(
            "<HHHHH", response_command, DATA_TYPE_CONTINUOUS, 0, 0xFFFF, error_code
        )
        return build_header(
            len(body), invoke_id, source=SOURCE_SERVER, plc_info=0x0101, cpu_info=0xA0
        ) + body

    def _parse_address(self, address: str) -> tuple[str, int]:
        match = _ADDRESS_RE.fullmatch(address)
        if not match:
            raise ValueError("Unsupported simulator address")
        return match.group(1), int(match.group(2))

    @staticmethod
    def _response_command(command: int) -> int:
        if command == CMD_WRITE_REQUEST:
            return CMD_WRITE_RESPONSE
        return CMD_READ_RESPONSE

    def _area(self, device: str) -> bytearray:
        with self._lock:
            if device not in self._areas:
                self._areas[device] = bytearray(self.memory_size)
            return self._areas[device]

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.stop()
