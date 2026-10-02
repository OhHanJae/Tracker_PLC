"""Thread-safe runtime status snapshot."""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RuntimeStatus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "service_started_at": utc_now(),
            "plc_state": "starting",
            "plc_endpoint": "",
            "connected_since": None,
            "retry_delay_ms": 0,
            "read_count": 0,
            "write_count": 0,
            "reconnect_count": 0,
            "last_read_at": None,
            "last_write_at": None,
            "last_error_at": None,
            "last_error": None,
            "last_error_code": 0,
            "read_address_normalized": "",
            "write_address_normalized": "",
            "web_running": False,
            "web_url": None,
            "control_running": False,
            "control_endpoint": None,
        }

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)

    def increment(self, key: str, amount: int = 1) -> int:
        with self._lock:
            self._data[key] = int(self._data.get(key, 0)) + amount
            return self._data[key]

    def error(self, message: str, code: int = -1) -> None:
        with self._lock:
            self._data.update(
                last_error=str(message),
                last_error_code=int(code),
                last_error_at=utc_now(),
            )

    def clear_error(self) -> None:
        with self._lock:
            self._data.update(last_error=None, last_error_code=0, last_error_at=None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

