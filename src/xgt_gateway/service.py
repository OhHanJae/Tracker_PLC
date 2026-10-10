"""Top-level gateway orchestration."""

from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Any

from .config import ConfigStore
from .control_server import ControlServer
from .plc_worker import PlcWorker
from .shared_memory_bridge import SharedMemoryBridge
from .status import RuntimeStatus
from .web_server import WebServer

LOGGER = logging.getLogger(__name__)


class GatewayService:
    def __init__(self, config_store: ConfigStore, config: dict[str, Any]) -> None:
        self._store = config_store
        self._config = copy.deepcopy(config)
        self._lock = threading.RLock()
        self.status = RuntimeStatus()
        self._bridge = SharedMemoryBridge(
            config["shared_memory"], config["read"]["byte_count"], config["write"]["byte_count"]
        )
        self._worker = PlcWorker(self.config_snapshot, self.shared_memory, self.status)
        control = config["control"]
        self._control = ControlServer(
            control["host"],
            control["port"],
            self._max_control_request,
            self.dispatch_command,
        )
        self._web: WebServer | None = None

    def start(self) -> None:
        self._worker.start()
        self._control.start()
        address = self._control.address
        self.status.update(
            control_running=True,
            control_endpoint=f"{address[0]}:{address[1]}" if address else None,
        )
        try:
            self._apply_web_config(self.config_snapshot()["web"])
        except Exception as exc:
            # Keep the TCP control server alive so the port can be corrected
            # remotely even when the configured HTTP port is unavailable.
            LOGGER.error("Web server failed to start: %s", exc)
            self.status.error(f"Web server failed to start: {exc}")
            self.status.update(web_running=False, web_url=None)

    def stop(self) -> None:
        if self._web:
            self._web.stop()
            self._web = None
        self.status.update(web_running=False, web_url=None)
        self._control.stop()
        self.status.update(control_running=False, control_endpoint=None)
        self._worker.stop()
        self._bridge.close()

    def config_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._config)

    def public_config(self) -> dict[str, Any]:
        config = self.config_snapshot()
        config["control"]["auth_token"] = ""
        return config

    def shared_memory(self) -> SharedMemoryBridge:
        return self._bridge

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            before = copy.deepcopy(self._config)
            updated = self._store.preview_update(patch)
            shm_changed = (
                before["shared_memory"] != updated["shared_memory"]
                or before["read"]["byte_count"] != updated["read"]["byte_count"]
                or before["write"]["byte_count"] != updated["write"]["byte_count"]
            )
            web_changed = before["web"] != updated["web"]
            try:
                if shm_changed:
                    self._bridge.reconfigure(
                        updated["shared_memory"],
                        updated["read"]["byte_count"],
                        updated["write"]["byte_count"],
                    )
                if web_changed:
                    self._apply_web_config(updated["web"])
                # Persist only after runtime resources accepted the new setup.
                self._store.replace(updated)
                self._config = updated
            except Exception:
                LOGGER.exception("Configuration apply failed; restoring previous runtime configuration")
                self._config = before
                rollback_errors: list[str] = []
                if shm_changed:
                    try:
                        self._bridge.reconfigure(
                            before["shared_memory"],
                            before["read"]["byte_count"],
                            before["write"]["byte_count"],
                        )
                    except Exception as rollback_exc:
                        rollback_errors.append(f"shared memory: {rollback_exc}")
                if web_changed:
                    try:
                        self._apply_web_config(before["web"])
                    except Exception as rollback_exc:
                        rollback_errors.append(f"web server: {rollback_exc}")
                if rollback_errors:
                    LOGGER.critical("Configuration rollback was incomplete: %s", "; ".join(rollback_errors))
                if shm_changed:
                    # Reopening the restored buffer clears its connection
                    # flags and sequences, so restart the worker's tracking.
                    self._worker.request_reconfigure()
                raise

            worker_changed = any(
                before[section] != updated[section]
                for section in ("plc", "read", "write", "shared_memory")
            )
            if worker_changed:
                self._worker.request_reconfigure()

            restart_required: list[str] = []
            if before["control"] != updated["control"]:
                restart_required.append("control_server")
            if before["logging"] != updated["logging"]:
                restart_required.append("logging")
            return {"config": self.public_config(), "restart_required": restart_required}

    def dispatch_command(self, command: str, params: dict[str, Any]) -> Any:
        if command == "ping":
            return {"message": "pong"}
        if command == "get_status":
            return self._combined_status()
        if command == "get_config":
            return self.public_config()
        if command == "update_config":
            patch = params.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("params.patch must be a JSON object")
            return self.update_config(patch)
        if command == "set_web_enabled":
            enabled = params.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("params.enabled must be true or false")
            return self.update_config({"web": {"enabled": enabled}})
        if command == "set_plc_enabled":
            enabled = params.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("params.enabled must be true or false")
            return self.update_config({"plc": {"enabled": enabled}})
        if command == "reconnect_plc":
            self._worker.request_reconnect()
            return {"requested": True}
        if command == "get_shared_memory":
            return self._bridge.info()
        if command == "read_shared_memory":
            area = params.get("area", "read")
            if area == "read":
                data, sequence = self._bridge.read_read_area()
            elif area == "write":
                data, sequence = self._bridge.read_write_area()
            else:
                raise ValueError("params.area must be 'read' or 'write'")
            return {"area": area, "sequence": sequence, "hex": data.hex(" ").upper(), "length": len(data)}
        if command == "write_shared_memory":
            hex_text = params.get("hex")
            if not isinstance(hex_text, str):
                raise ValueError("params.hex must be a hexadecimal string")
            try:
                data = bytes.fromhex(hex_text)
            except ValueError as exc:
                raise ValueError(f"Invalid hexadecimal data: {exc}") from exc
            sequence = self._bridge.commit_write(data)
            return {"sequence": sequence, "length": len(data)}
        raise ValueError(f"Unknown command: {command}")

    def dispatch_http(self, method: str, path: str, body: dict[str, Any]) -> Any:
        if method == "GET" and path == "/api/status":
            return self._combined_status()
        if method == "GET" and path == "/api/config":
            return self.public_config()
        if method == "PUT" and path == "/api/config":
            patch = body.get("patch", body)
            if not isinstance(patch, dict):
                raise ValueError("Configuration patch must be an object")
            return self.update_config(patch)
        if method == "POST" and path == "/api/plc/reconnect":
            return self.dispatch_command("reconnect_plc", {})
        if method == "POST" and path == "/api/plc/enabled":
            return self.dispatch_command("set_plc_enabled", body)
        if method == "POST" and path == "/api/web/enabled":
            return self.dispatch_command("set_web_enabled", body)
        if method == "GET" and path == "/api/shm":
            return self.dispatch_command("get_shared_memory", {})
        if method == "POST" and path == "/api/shm/read":
            return self.dispatch_command("read_shared_memory", body)
        if method == "POST" and path == "/api/shm/write":
            return self.dispatch_command("write_shared_memory", body)
        raise KeyError(path)

    def _combined_status(self) -> dict[str, Any]:
        result = self.status.snapshot()
        result["shared_memory"] = self._bridge.info()
        return result

    def _apply_web_config(self, config: dict[str, Any]) -> None:
        if self._web:
            self._web.stop()
            self._web = None
        if config["enabled"]:
            candidate = WebServer(
                config["host"], config["port"], self.dispatch_http
            )
            candidate.start()
            self._web = candidate
            address = self._web.address
            display_host = "127.0.0.1" if address and address[0] in {"0.0.0.0", "::"} else address[0]
            self.status.update(
                web_running=True,
                web_url=f"http://{display_host}:{address[1]}/" if address else None,
            )
        else:
            self.status.update(web_running=False, web_url=None)

    def _max_control_request(self) -> int:
        return int(self.config_snapshot()["control"]["max_request_bytes"])
