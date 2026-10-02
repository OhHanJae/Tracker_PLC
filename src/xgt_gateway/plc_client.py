"""Blocking TCP client for XGT continuous-byte transactions."""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from .errors import XgtProtocolError
from .protocol import (
    HEADER_SIZE,
    build_continuous_read_request,
    build_continuous_write_request,
    parse_continuous_read_response,
    parse_continuous_write_response,
    parse_header,
    split_frame,
)

LOGGER = logging.getLogger(__name__)


class XgtTcpClient:
    """One-request-at-a-time XGT/TCP connection."""

    def __init__(self, plc_config: dict[str, Any]) -> None:
        self._config = dict(plc_config)
        self._socket: socket.socket | None = None
        self._invoke_id = 0
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection(
            (self._config["host"], self._config["port"]),
            timeout=float(self._config["connect_timeout_s"]),
        )
        sock.settimeout(float(self._config["io_timeout_s"]))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._enable_keepalive(sock)
        self._socket = sock

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def read_continuous(self, address: str, byte_count: int) -> bytes:
        with self._lock:
            invoke_id = self._next_invoke_id()
            request = build_continuous_read_request(
                address,
                byte_count,
                invoke_id,
                cpu_info=self._config["cpu_info"],
                slot=self._config["slot"],
                base=self._config["base"],
                use_bcc=self._config["use_bcc"],
            )
            frame = self._exchange(request)
            return parse_continuous_read_response(frame, invoke_id, byte_count)

    def write_continuous(self, address: str, data: bytes) -> None:
        with self._lock:
            invoke_id = self._next_invoke_id()
            request = build_continuous_write_request(
                address,
                data,
                invoke_id,
                cpu_info=self._config["cpu_info"],
                slot=self._config["slot"],
                base=self._config["base"],
                use_bcc=self._config["use_bcc"],
            )
            frame = self._exchange(request)
            parse_continuous_write_response(frame, invoke_id)

    def _next_invoke_id(self) -> int:
        self._invoke_id = (self._invoke_id + 1) & 0xFFFF
        return self._invoke_id

    def _exchange(self, request: bytes):
        if self._socket is None:
            raise ConnectionError("PLC socket is not connected")
        try:
            self._socket.sendall(request)
            raw_header = self._recv_exact(HEADER_SIZE)
            validate_bcc = bool(self._config.get("use_bcc", True))
            header = parse_header(raw_header, validate_bcc=validate_bcc)
            if header.length > 65_535:
                raise XgtProtocolError(f"Invalid XGT payload length: {header.length}")
            payload = self._recv_exact(header.length)
            return split_frame(raw_header + payload, validate_bcc=validate_bcc)
        except Exception:
            self.close()
            raise

    def _recv_exact(self, size: int) -> bytes:
        if self._socket is None:
            raise ConnectionError("PLC socket is not connected")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._socket.recv(remaining)
            if not chunk:
                raise ConnectionError("PLC closed the TCP connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _enable_keepalive(sock: socket.socket) -> None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        keepalive_options = (
            ("TCP_KEEPIDLE", 30),
            ("TCP_KEEPINTVL", 10),
            ("TCP_KEEPCNT", 3),
            ("TCP_KEEPALIVE", 30),
        )
        for option_name, value in keepalive_options:
            option = getattr(socket, option_name, None)
            if option is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError as exc:
                LOGGER.debug("TCP keepalive option %s unsupported: %s", option_name, exc)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()
