"""PLC polling and shared-memory transfer worker."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .addressing import normalize_continuous_byte_address
from .errors import XgtPlcError
from .plc_client import XgtTcpClient
from .shared_memory_bridge import SharedMemoryBridge
from .status import RuntimeStatus, utc_now

LOGGER = logging.getLogger(__name__)


class PlcWorker:
    def __init__(
        self,
        config_provider: Callable[[], dict[str, Any]],
        shared_memory_provider: Callable[[], SharedMemoryBridge],
        status: RuntimeStatus,
    ) -> None:
        self._config_provider = config_provider
        self._shared_memory_provider = shared_memory_provider
        self._status = status
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._reconfigure_event = threading.Event()
        self._reconnect_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: XgtTcpClient | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="xgt-plc-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._client:
            self._client.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)

    def request_reconfigure(self) -> None:
        self._reconfigure_event.set()
        self._wake_event.set()

    def request_reconnect(self) -> None:
        self._reconnect_event.set()
        self._wake_event.set()

    def _run(self) -> None:
        client: XgtTcpClient | None = None
        retry_delay_ms = 0
        next_read = 0.0
        next_write = 0.0
        last_written_sequence = 0
        write_armed = False
        write_tracking_initialized = False
        self._shared_memory_provider().set_status(connected=False, read_ok=False, write_ok=False)

        while not self._stop_event.is_set():
            config = self._config_provider()
            plc_config = config["plc"]
            bridge = self._shared_memory_provider()

            if self._reconfigure_event.is_set():
                self._reconfigure_event.clear()
                if client:
                    client.close()
                client = None
                next_read = next_write = 0.0
                write_tracking_initialized = False

            if self._reconnect_event.is_set():
                self._reconnect_event.clear()
                if client:
                    client.close()
                client = None
                next_read = next_write = 0.0

            if not write_tracking_initialized:
                try:
                    _, sequence = bridge.read_write_area()
                    acknowledged = bridge.info()["write_ack_sequence"]
                except Exception:
                    sequence = acknowledged = 0
                last_written_sequence = acknowledged
                write_armed = bool(config["write"]["write_on_startup"]) or sequence != acknowledged
                if config["write"]["write_on_startup"] and sequence == acknowledged:
                    # Force one startup write while preserving the normal
                    # sequence-change rule for all later writes.
                    last_written_sequence = (sequence - 2) & 0xFFFFFFFF
                write_tracking_initialized = True

            self._status.update(
                plc_endpoint=f"{plc_config['host']}:{plc_config['port']}",
                read_address_normalized=normalize_continuous_byte_address(config["read"]["address"]),
                write_address_normalized=normalize_continuous_byte_address(config["write"]["address"]),
            )

            if not plc_config["enabled"]:
                if client:
                    client.close()
                    client = None
                bridge.set_status(connected=False)
                self._status.update(plc_state="stopped", connected_since=None, retry_delay_ms=0)
                self._wait(0.25)
                continue

            if client is None:
                try:
                    self._status.update(plc_state="connecting")
                    client = XgtTcpClient(plc_config)
                    self._client = client
                    client.connect()
                    if self._stop_event.is_set():
                        break
                    retry_delay_ms = int(plc_config["retry_initial_ms"])
                    now = time.monotonic()
                    next_read = now
                    next_write = now
                    bridge.set_status(connected=True, read_ok=False, write_ok=False, error_code=0)
                    self._status.clear_error()
                    self._status.update(
                        plc_state="connected",
                        connected_since=utc_now(),
                        retry_delay_ms=0,
                    )
                    LOGGER.info("Connected to PLC %s:%s", plc_config["host"], plc_config["port"])
                except Exception as exc:
                    if client:
                        client.close()
                    client = None
                    self._client = None
                    if self._stop_event.is_set():
                        break
                    retry_delay_ms = self._next_retry(retry_delay_ms, plc_config)
                    self._handle_connection_error(exc, bridge, retry_delay_ms)
                    self._wait(retry_delay_ms / 1000.0)
                    continue

            try:
                now = time.monotonic()
                did_work = False

                read_config = config["read"]
                if read_config["enabled"] and now >= next_read:
                    data = client.read_continuous(read_config["address"], read_config["byte_count"])
                    bridge.publish_read(data)
                    bridge.set_status(read_ok=True, error_code=0)
                    self._status.increment("read_count")
                    self._status.update(last_read_at=utc_now())
                    next_read = time.monotonic() + read_config["interval_ms"] / 1000.0
                    did_work = True

                write_config = config["write"]
                if write_config["enabled"] and now >= next_write:
                    write_data, write_sequence = bridge.read_write_area()
                    sequence_changed = write_sequence != last_written_sequence
                    if sequence_changed:
                        write_armed = True
                    should_write = sequence_changed if write_config["mode"] == "on_change" else write_armed
                    if should_write:
                        client.write_continuous(write_config["address"], write_data)
                        bridge.acknowledge_write(write_sequence)
                        bridge.set_status(write_ok=True, error_code=0)
                        last_written_sequence = write_sequence
                        self._status.increment("write_count")
                        self._status.update(last_write_at=utc_now())
                    next_write = time.monotonic() + write_config["interval_ms"] / 1000.0
                    did_work = True

                if not did_work:
                    due_times: list[float] = []
                    if read_config["enabled"]:
                        due_times.append(next_read)
                    if write_config["enabled"]:
                        due_times.append(next_write)
                    delay = max(0.001, min(due_times) - time.monotonic()) if due_times else 0.25
                    self._wait(min(delay, 0.25))
            except Exception as exc:
                if client:
                    client.close()
                client = None
                self._client = None
                if self._stop_event.is_set():
                    break
                retry_delay_ms = self._next_retry(retry_delay_ms, plc_config)
                self._handle_connection_error(exc, bridge, retry_delay_ms)
                self._wait(retry_delay_ms / 1000.0)

        if client:
            client.close()
        self._client = None
        try:
            self._shared_memory_provider().set_status(connected=False)
        except Exception:
            pass
        self._status.update(plc_state="stopped", connected_since=None)

    def _handle_connection_error(self, exc: Exception, bridge: SharedMemoryBridge, retry_delay_ms: int) -> None:
        error_code = exc.error_code if isinstance(exc, XgtPlcError) else -1
        message = f"{type(exc).__name__}: {exc}"
        LOGGER.warning("PLC communication failed: %s", message)
        bridge.set_status(connected=False, read_ok=False, write_ok=False, error_code=error_code)
        self._status.error(message, error_code)
        self._status.increment("reconnect_count")
        self._status.update(
            plc_state="retry_wait",
            connected_since=None,
            retry_delay_ms=retry_delay_ms,
        )

    @staticmethod
    def _next_retry(current_ms: int, plc_config: dict[str, Any]) -> int:
        initial = int(plc_config["retry_initial_ms"])
        maximum = int(plc_config["retry_max_ms"])
        if current_ms <= 0:
            return initial
        return min(maximum, max(initial, int(current_ms * float(plc_config["retry_multiplier"]))))

    def _wait(self, seconds: float) -> None:
        self._wake_event.wait(max(0.0, seconds))
        self._wake_event.clear()
