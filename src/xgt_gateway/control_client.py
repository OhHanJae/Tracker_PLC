"""Synchronous client for the JSON-lines control protocol."""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any


def request(
    host: str,
    port: int,
    *args: Any,
    params: dict[str, Any] | None = None,
    token: str = "",
    timeout: float = 5.0,
) -> Any:
    command, params = _parse_request_args(args, params)
    request_id = uuid.uuid4().hex
    payload = {
        "id": request_id,
        "command": command,
        "params": params or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(encoded)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Control server closed the connection before a response")
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunks.append(chunk[:newline])
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > 16_777_216:
                raise ValueError("Control response exceeded 16 MiB")
    response = json.loads(b"".join(chunks).decode("utf-8"))
    if response.get("id") != request_id:
        raise ValueError("Control response ID does not match the request")
    if not response.get("ok"):
        raise RuntimeError(response.get("message") or response.get("error") or "Control request failed")
    return response.get("result")


def _parse_request_args(
    args: tuple[Any, ...],
    keyword_params: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None]:
    if len(args) == 1:
        command = args[0]
        params = keyword_params
    elif len(args) == 2:
        if isinstance(args[1], str):
            command = args[1]
            params = keyword_params
        else:
            command = args[0]
            params = args[1]
    elif len(args) == 3:
        command = args[1]
        params = args[2]
    else:
        raise TypeError("request() expects command[, params] or token, command[, params]")

    if not isinstance(command, str):
        raise TypeError("command must be a string")
    if params is not None and not isinstance(params, dict):
        raise TypeError("params must be a dictionary")
    return command, params
