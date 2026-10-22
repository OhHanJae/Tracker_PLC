"""Single TX producer lock shared by Core and Gateway diagnostic writers."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class TxWriterLock:
    def __init__(self, name: str) -> None:
        canonical = name.removeprefix("Local\\").lstrip("/")
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / ("tracker-xgt-tx-" + digest + ".lock")
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        handle = self.path.open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise OSError("TX producer is already active; stop Core before diagnostic writes") from exc
        self._handle = handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.close()
