"""Persistent JSON configuration and validation."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .addressing import normalize_continuous_byte_address
from .errors import ConfigurationError

SCHEMA_VERSION = 1


def default_config() -> dict[str, Any]:
    """Build a fresh default configuration."""

    return {
        "schema_version": SCHEMA_VERSION,
        "plc": {
            "enabled": True,
            "host": "192.168.0.10",
            "port": 2004,
            "cpu_info": 0xA0,
            "slot": 0,
            "base": 0,
            "use_bcc": True,
            "connect_timeout_s": 3.0,
            "io_timeout_s": 2.0,
            "retry_initial_ms": 500,
            "retry_max_ms": 10_000,
            "retry_multiplier": 2.0,
        },
        "read": {
            "enabled": True,
            "address": "D9000",
            "byte_count": 200,
            "interval_ms": 50,
        },
        "write": {
            "enabled": True,
            "address": "D100",
            "byte_count": 200,
            "interval_ms": 200,
            "mode": "cyclic",
            "write_on_startup": False,
        },
        "shared_memory": {
            "name": "xgt_gateway_v1",
            "size": 512,
            "read_offset": 64,
            "write_offset": 264,
            "unlink_on_exit": True,
        },
        "control": {
            "host": "0.0.0.0",
            "port": 5150,
            "auth_token": "",
            "max_request_bytes": 1_048_576,
        },
        "web": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 5051,
        },
        "logging": {
            "level": "INFO",
            "file": "logs/xgt_gateway.log",
            "max_bytes": 2_000_000,
            "backup_count": 5,
        },
    }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any], path: str = "") -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        dotted = f"{path}.{key}" if path else key
        if key not in base:
            raise ConfigurationError(f"Unknown configuration key: {dotted}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigurationError(f"Configuration section must be an object: {dotted}")
            result[key] = _deep_merge(base[key], value, dotted)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _require_bool(value: Any, name: str) -> None:
    if type(value) is not bool:
        raise ConfigurationError(f"{name} must be true or false")


def _require_number(value: Any, name: str, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    if not minimum <= float(value) <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")


def _require_int(value: Any, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")


def _binds_overlap(first_host: str, first_port: int, second_host: str, second_port: int) -> bool:
    if first_port != second_port:
        return False
    first = first_host.strip().lower()
    second = second_host.strip().lower()
    wildcards = {"", "0.0.0.0", "::"}
    return first == second or first in wildcards or second in wildcards


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a defensive copy of *config*."""

    # Reject unknown/missing top-level and nested keys by merging onto defaults.
    merged = _deep_merge(default_config(), config)
    if set(config) != set(merged):
        raise ConfigurationError("Configuration is missing required top-level sections")
    for section in merged:
        if isinstance(merged[section], dict) and set(config.get(section, {})) != set(merged[section]):
            raise ConfigurationError(f"Configuration section {section!r} is incomplete")

    if merged["schema_version"] != SCHEMA_VERSION:
        raise ConfigurationError(f"Unsupported schema_version: {merged['schema_version']}")

    plc = merged["plc"]
    _require_bool(plc["enabled"], "plc.enabled")
    if not isinstance(plc["host"], str) or not plc["host"].strip():
        raise ConfigurationError("plc.host must be a non-empty hostname or IP address")
    _require_int(plc["port"], "plc.port", 1, 65535)
    _require_int(plc["cpu_info"], "plc.cpu_info", 0, 255)
    _require_int(plc["slot"], "plc.slot", 0, 15)
    _require_int(plc["base"], "plc.base", 0, 15)
    _require_bool(plc["use_bcc"], "plc.use_bcc")
    _require_number(plc["connect_timeout_s"], "plc.connect_timeout_s", 0.1, 120.0)
    _require_number(plc["io_timeout_s"], "plc.io_timeout_s", 0.1, 120.0)
    _require_int(plc["retry_initial_ms"], "plc.retry_initial_ms", 50, 600_000)
    _require_int(plc["retry_max_ms"], "plc.retry_max_ms", 50, 3_600_000)
    _require_number(plc["retry_multiplier"], "plc.retry_multiplier", 1.0, 10.0)
    if plc["retry_max_ms"] < plc["retry_initial_ms"]:
        raise ConfigurationError("plc.retry_max_ms must be >= plc.retry_initial_ms")

    for section_name in ("read", "write"):
        section = merged[section_name]
        _require_bool(section["enabled"], f"{section_name}.enabled")
        normalize_continuous_byte_address(section["address"])
        _require_int(section["byte_count"], f"{section_name}.byte_count", 1, 1400)
        _require_int(section["interval_ms"], f"{section_name}.interval_ms", 10, 3_600_000)

    write = merged["write"]
    if write["mode"] not in {"on_change", "cyclic"}:
        raise ConfigurationError("write.mode must be 'on_change' or 'cyclic'")
    _require_bool(write["write_on_startup"], "write.write_on_startup")

    shm = merged["shared_memory"]
    if not isinstance(shm["name"], str) or not shm["name"]:
        raise ConfigurationError("shared_memory.name must be a non-empty string")
    if len(shm["name"]) > 64 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in shm["name"]):
        raise ConfigurationError("shared_memory.name may contain only A-Z, a-z, 0-9, _, -, and . (max 64)")
    _require_int(shm["size"], "shared_memory.size", 128, 1_048_576)
    _require_int(shm["read_offset"], "shared_memory.read_offset", 64, shm["size"] - 1)
    _require_int(shm["write_offset"], "shared_memory.write_offset", 64, shm["size"] - 1)
    _require_bool(shm["unlink_on_exit"], "shared_memory.unlink_on_exit")
    read_range = (shm["read_offset"], shm["read_offset"] + merged["read"]["byte_count"])
    write_range = (shm["write_offset"], shm["write_offset"] + merged["write"]["byte_count"])
    if read_range[1] > shm["size"]:
        raise ConfigurationError("Read area exceeds shared_memory.size")
    if write_range[1] > shm["size"]:
        raise ConfigurationError("Write area exceeds shared_memory.size")
    if max(read_range[0], write_range[0]) < min(read_range[1], write_range[1]):
        raise ConfigurationError("Shared-memory read and write areas overlap")

    control = merged["control"]
    if not isinstance(control["host"], str) or not control["host"].strip():
        raise ConfigurationError("control.host must be a non-empty bind address")
    _require_int(control["port"], "control.port", 1, 65535)
    if not isinstance(control["auth_token"], str):
        raise ConfigurationError("control.auth_token must be a string")
    _require_int(control["max_request_bytes"], "control.max_request_bytes", 1024, 16_777_216)

    web = merged["web"]
    _require_bool(web["enabled"], "web.enabled")
    if not isinstance(web["host"], str) or not web["host"].strip():
        raise ConfigurationError("web.host must be a non-empty bind address")
    _require_int(web["port"], "web.port", 1, 65535)
    if _binds_overlap(web["host"], web["port"], control["host"], control["port"]):
        raise ConfigurationError("web and control servers cannot use the same bind address and port")

    logging = merged["logging"]
    if logging["level"] not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("logging.level is invalid")
    if not isinstance(logging["file"], str):
        raise ConfigurationError("logging.file must be a string")
    _require_int(logging["max_bytes"], "logging.max_bytes", 10_000, 1_000_000_000)
    _require_int(logging["backup_count"], "logging.backup_count", 0, 100)
    return copy.deepcopy(merged)


class ConfigStore:
    """Thread-safe, atomically persisted JSON configuration."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}

    def load_or_create(self) -> tuple[dict[str, Any], bool]:
        """Load the file, or create defaults. Returns ``(config, created)``."""

        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ConfigurationError(f"Unable to read {self.path}: {exc}") from exc
                self._config = validate_config(raw)
                return copy.deepcopy(self._config), False
            created_config = validate_config(default_config())
            self._save_value_locked(created_config)
            self._config = created_config
            return copy.deepcopy(self._config), True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._config)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ConfigurationError("Configuration patch must be a JSON object")
        with self._lock:
            candidate = _deep_merge(self._config, patch)
            candidate = validate_config(candidate)
            self._save_value_locked(candidate)
            self._config = candidate
            return copy.deepcopy(candidate)

    def preview_update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Validate a partial update without changing memory or disk."""

        if not isinstance(patch, dict):
            raise ConfigurationError("Configuration patch must be a JSON object")
        with self._lock:
            return validate_config(_deep_merge(self._config, patch))

    def replace(self, config: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            candidate = validate_config(config)
            self._save_value_locked(candidate)
            self._config = candidate
            return copy.deepcopy(self._config)

    def _save_value_locked(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
