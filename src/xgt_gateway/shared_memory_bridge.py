"""Cross-platform shared-memory layout used by the gateway."""

from __future__ import annotations

import os
import struct
import threading
import time
from multiprocessing import shared_memory
from typing import Any

from .errors import ConfigurationError

MAGIC = b"XGSM"
LAYOUT_VERSION = 1
HEADER_SIZE = 64

OFF_MAGIC = 0
OFF_VERSION = 4
OFF_HEADER_SIZE = 6
OFF_TOTAL_SIZE = 8
OFF_READ_OFFSET = 12
OFF_READ_LENGTH = 16
OFF_WRITE_OFFSET = 20
OFF_WRITE_LENGTH = 24
OFF_READ_SEQUENCE = 28
OFF_WRITE_SEQUENCE = 32
OFF_WRITE_ACK_SEQUENCE = 36
OFF_STATUS_FLAGS = 40
OFF_LAST_ERROR = 44
OFF_LAST_READ_NS = 48
OFF_LAST_WRITE_NS = 56

FLAG_PLC_CONNECTED = 1 << 0
FLAG_LAST_READ_OK = 1 << 1
FLAG_LAST_WRITE_OK = 1 << 2


def _open_shared_memory(*, name: str, create: bool, size: int | None = None, track: bool = True) -> shared_memory.SharedMemory:
    kwargs: dict[str, Any] = {"name": name, "create": create}
    if size is not None:
        kwargs["size"] = size
    try:
        return shared_memory.SharedMemory(**kwargs, track=track)  # type: ignore[call-arg]
    except TypeError:
        shm = shared_memory.SharedMemory(**kwargs)
        if not track:
            _unregister_resource_tracker(shm)
        return shm


def _unregister_resource_tracker(shm: shared_memory.SharedMemory) -> None:
    """Keep POSIX resource_tracker from unlinking intentionally persistent SHM."""

    if os.name == "nt":
        return
    try:
        from multiprocessing import resource_tracker

        resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
    except Exception:
        pass


class SharedMemoryBridge:
    """Owns or attaches to one named shared-memory segment.

    Read and write buffers use a simple sequence-lock convention.  A producer
    sets the sequence to an odd value, copies bytes, then sets it to the next
    even value.  Consumers accept data only when the sequence is unchanged and
    even before/after copying.
    """

    def __init__(self, config: dict[str, Any], read_length: int, write_length: int) -> None:
        self._lock = threading.RLock()
        self._shm: shared_memory.SharedMemory | None = None
        self._config: dict[str, Any] = {}
        self._read_length = 0
        self._write_length = 0
        self._created = False
        self.open(config, read_length, write_length)

    @property
    def name(self) -> str:
        with self._lock:
            return self._require_open().name

    def open(self, config: dict[str, Any], read_length: int, write_length: int) -> None:
        with self._lock:
            self.close(unlink=False)
            self._config = dict(config)
            self._read_length = int(read_length)
            self._write_length = int(write_length)
            name = self._config["name"]
            size = self._config["size"]
            track = bool(self._config.get("unlink_on_exit", True))
            try:
                shm = _open_shared_memory(name=name, create=True, size=size, track=track)
                created = True
            except FileExistsError:
                shm = _open_shared_memory(name=name, create=False, track=track)
                created = False
            self._shm = shm
            self._created = created
            try:
                if created:
                    shm.buf[:] = b"\x00" * shm.size
                    self._initialize_header()
                else:
                    self._validate_existing_header()
            except Exception:
                self._shm = None
                shm.close()
                if created:
                    try:
                        shm.unlink()
                    except FileNotFoundError:
                        pass
                raise

    def reconfigure(self, config: dict[str, Any], read_length: int, write_length: int) -> None:
        with self._lock:
            old = self._shm
            old_config = self._config
            if old is not None:
                self.set_status(connected=False, read_ok=False, write_ok=False)
                old.close()
                if old_config.get("unlink_on_exit"):
                    try:
                        old.unlink()
                    except FileNotFoundError:
                        pass
                self._shm = None
            self.open(config, read_length, write_length)

    def close(self, *, unlink: bool | None = None) -> None:
        with self._lock:
            shm, self._shm = self._shm, None
            if shm is None:
                return
            try:
                shm.close()
            finally:
                should_unlink = self._config.get("unlink_on_exit", True) if unlink is None else unlink
                if should_unlink:
                    try:
                        shm.unlink()
                    except FileNotFoundError:
                        pass

    def publish_read(self, data: bytes) -> int:
        payload = bytes(data)
        if len(payload) != self._read_length:
            raise ValueError(f"Read payload must be {self._read_length} bytes")
        with self._lock:
            shm = self._require_open()
            sequence = self._begin_write(shm.buf, OFF_READ_SEQUENCE)
            start = self._config["read_offset"]
            shm.buf[start : start + self._read_length] = payload
            final_sequence = (sequence + 1) & 0xFFFFFFFF
            struct.pack_into("<I", shm.buf, OFF_READ_SEQUENCE, final_sequence)
            struct.pack_into("<Q", shm.buf, OFF_LAST_READ_NS, time.time_ns())
            return final_sequence

    def read_read_area(self, attempts: int = 20) -> tuple[bytes, int]:
        with self._lock:
            return self._stable_read(OFF_READ_SEQUENCE, self._config["read_offset"], self._read_length, attempts)

    def commit_write(self, data: bytes) -> int:
        """Copy a producer payload into TX memory and commit it."""

        payload = bytes(data)
        if len(payload) != self._write_length:
            raise ValueError(f"Write payload must be {self._write_length} bytes")
        with self._lock:
            shm = self._require_open()
            sequence = self._begin_write(shm.buf, OFF_WRITE_SEQUENCE)
            start = self._config["write_offset"]
            shm.buf[start : start + self._write_length] = payload
            final_sequence = (sequence + 1) & 0xFFFFFFFF
            struct.pack_into("<I", shm.buf, OFF_WRITE_SEQUENCE, final_sequence)
            return final_sequence

    def read_write_area(self, attempts: int = 20) -> tuple[bytes, int]:
        with self._lock:
            return self._stable_read(
                OFF_WRITE_SEQUENCE, self._config["write_offset"], self._write_length, attempts
            )

    def acknowledge_write(self, sequence: int) -> None:
        with self._lock:
            shm = self._require_open()
            struct.pack_into("<I", shm.buf, OFF_WRITE_ACK_SEQUENCE, sequence & 0xFFFFFFFF)
            struct.pack_into("<Q", shm.buf, OFF_LAST_WRITE_NS, time.time_ns())

    def set_status(self, *, connected: bool | None = None, read_ok: bool | None = None, write_ok: bool | None = None, error_code: int | None = None) -> None:
        with self._lock:
            shm = self._require_open()
            flags = struct.unpack_from("<I", shm.buf, OFF_STATUS_FLAGS)[0]
            flags = self._set_flag(flags, FLAG_PLC_CONNECTED, connected)
            flags = self._set_flag(flags, FLAG_LAST_READ_OK, read_ok)
            flags = self._set_flag(flags, FLAG_LAST_WRITE_OK, write_ok)
            struct.pack_into("<I", shm.buf, OFF_STATUS_FLAGS, flags)
            if error_code is not None:
                struct.pack_into("<i", shm.buf, OFF_LAST_ERROR, int(error_code))

    def info(self) -> dict[str, Any]:
        with self._lock:
            shm = self._require_open()
            return {
                "name": shm.name,
                "size": self._config["size"],
                "header_size": HEADER_SIZE,
                "layout_version": LAYOUT_VERSION,
                "read_offset": self._config["read_offset"],
                "read_length": self._read_length,
                "write_offset": self._config["write_offset"],
                "write_length": self._write_length,
                "read_sequence": struct.unpack_from("<I", shm.buf, OFF_READ_SEQUENCE)[0],
                "write_sequence": struct.unpack_from("<I", shm.buf, OFF_WRITE_SEQUENCE)[0],
                "write_ack_sequence": struct.unpack_from("<I", shm.buf, OFF_WRITE_ACK_SEQUENCE)[0],
                "status_flags": struct.unpack_from("<I", shm.buf, OFF_STATUS_FLAGS)[0],
                "last_error_code": struct.unpack_from("<i", shm.buf, OFF_LAST_ERROR)[0],
                "last_read_time_ns": struct.unpack_from("<Q", shm.buf, OFF_LAST_READ_NS)[0],
                "last_write_time_ns": struct.unpack_from("<Q", shm.buf, OFF_LAST_WRITE_NS)[0],
            }

    def _initialize_header(self) -> None:
        shm = self._require_open()
        struct.pack_into(
            "<4sHH9IiQQ",
            shm.buf,
            0,
            MAGIC,
            LAYOUT_VERSION,
            HEADER_SIZE,
            self._config["size"],
            self._config["read_offset"],
            self._read_length,
            self._config["write_offset"],
            self._write_length,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    def _validate_existing_header(self) -> None:
        shm = self._require_open()
        if bytes(shm.buf[:4]) != MAGIC:
            raise ConfigurationError(
                f"Shared memory {shm.name!r} exists but does not contain an XGSM header"
            )
        version = struct.unpack_from("<H", shm.buf, OFF_VERSION)[0]
        header_size = struct.unpack_from("<H", shm.buf, OFF_HEADER_SIZE)[0]
        existing = (
            struct.unpack_from("<I", shm.buf, OFF_TOTAL_SIZE)[0],
            struct.unpack_from("<I", shm.buf, OFF_READ_OFFSET)[0],
            struct.unpack_from("<I", shm.buf, OFF_READ_LENGTH)[0],
            struct.unpack_from("<I", shm.buf, OFF_WRITE_OFFSET)[0],
            struct.unpack_from("<I", shm.buf, OFF_WRITE_LENGTH)[0],
        )
        expected = (
            self._config["size"],
            self._config["read_offset"],
            self._read_length,
            self._config["write_offset"],
            self._write_length,
        )
        legacy_windows_page_size = (
            os.name == "nt"
            and existing[0] == shm.size
            and expected[0] <= shm.size
            and existing[1:] == expected[1:]
        )
        if (
            version != LAYOUT_VERSION
            or header_size != HEADER_SIZE
            or (existing != expected and not legacy_windows_page_size)
        ):
            raise ConfigurationError(
                f"Shared memory {shm.name!r} has an incompatible layout. "
                "Stop the process using it or choose another shared-memory name."
            )
        if legacy_windows_page_size:
            struct.pack_into("<I", shm.buf, OFF_TOTAL_SIZE, expected[0])

    def _stable_read(self, sequence_offset: int, data_offset: int, length: int, attempts: int) -> tuple[bytes, int]:
        shm = self._require_open()
        for _ in range(attempts):
            before = struct.unpack_from("<I", shm.buf, sequence_offset)[0]
            if before & 1:
                time.sleep(0)
                continue
            data = bytes(shm.buf[data_offset : data_offset + length])
            after = struct.unpack_from("<I", shm.buf, sequence_offset)[0]
            if before == after and not (after & 1):
                return data, after
        raise RuntimeError("Shared-memory buffer remained busy during a stable read")

    @staticmethod
    def _begin_write(buffer: memoryview, sequence_offset: int) -> int:
        current = struct.unpack_from("<I", buffer, sequence_offset)[0]
        odd = ((current + 1) if not (current & 1) else (current + 2)) & 0xFFFFFFFF
        struct.pack_into("<I", buffer, sequence_offset, odd)
        return odd

    @staticmethod
    def _set_flag(flags: int, mask: int, value: bool | None) -> int:
        if value is None:
            return flags
        return flags | mask if value else flags & ~mask

    def _require_open(self) -> shared_memory.SharedMemory:
        if self._shm is None:
            raise RuntimeError("Shared memory is closed")
        return self._shm


class SharedMemoryClient:
    """Small helper for another Python process using the gateway memory."""

    def __init__(self, name: str) -> None:
        # Python 3.13 added track=False.  On 3.10-3.12, unregister attached
        # client handles on POSIX so a short-lived client does not unlink the
        # gateway-owned segment when it exits.
        self._shm = _open_shared_memory(name=name, create=False, track=False)
        self._validate()

    def close(self) -> None:
        self._shm.close()

    def layout(self) -> dict[str, int | str]:
        buf = self._shm.buf
        return {
            "name": self._shm.name,
            "size": struct.unpack_from("<I", buf, OFF_TOTAL_SIZE)[0],
            "read_offset": struct.unpack_from("<I", buf, OFF_READ_OFFSET)[0],
            "read_length": struct.unpack_from("<I", buf, OFF_READ_LENGTH)[0],
            "write_offset": struct.unpack_from("<I", buf, OFF_WRITE_OFFSET)[0],
            "write_length": struct.unpack_from("<I", buf, OFF_WRITE_LENGTH)[0],
        }

    def read_plc_data(self) -> bytes:
        layout = self.layout()
        return self._stable_read(OFF_READ_SEQUENCE, int(layout["read_offset"]), int(layout["read_length"]))[0]

    def write_plc_data(self, data: bytes) -> int:
        layout = self.layout()
        length = int(layout["write_length"])
        if len(data) != length:
            raise ValueError(f"Expected exactly {length} bytes")
        buf = self._shm.buf
        current = struct.unpack_from("<I", buf, OFF_WRITE_SEQUENCE)[0]
        odd = ((current + 1) if not (current & 1) else (current + 2)) & 0xFFFFFFFF
        struct.pack_into("<I", buf, OFF_WRITE_SEQUENCE, odd)
        offset = int(layout["write_offset"])
        buf[offset : offset + length] = data
        final = (odd + 1) & 0xFFFFFFFF
        struct.pack_into("<I", buf, OFF_WRITE_SEQUENCE, final)
        return final

    def write_acknowledged(self) -> bool:
        buf = self._shm.buf
        sequence = struct.unpack_from("<I", buf, OFF_WRITE_SEQUENCE)[0]
        ack = struct.unpack_from("<I", buf, OFF_WRITE_ACK_SEQUENCE)[0]
        return sequence == ack and not (sequence & 1)

    def _validate(self) -> None:
        if bytes(self._shm.buf[:4]) != MAGIC:
            self.close()
            raise RuntimeError("Shared memory does not contain an XGSM header")
        if struct.unpack_from("<H", self._shm.buf, OFF_VERSION)[0] != LAYOUT_VERSION:
            self.close()
            raise RuntimeError("Unsupported XGSM layout version")

    def _stable_read(self, sequence_offset: int, data_offset: int, length: int) -> tuple[bytes, int]:
        for _ in range(20):
            before = struct.unpack_from("<I", self._shm.buf, sequence_offset)[0]
            if before & 1:
                time.sleep(0)
                continue
            data = bytes(self._shm.buf[data_offset : data_offset + length])
            after = struct.unpack_from("<I", self._shm.buf, sequence_offset)[0]
            if before == after and not (after & 1):
                return data, after
        raise RuntimeError("Shared-memory buffer remained busy")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()
