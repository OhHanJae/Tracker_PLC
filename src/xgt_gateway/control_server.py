"""Newline-delimited JSON TCP control server."""

from __future__ import annotations

import json
import logging
import socketserver
import threading
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


class _ThreadingTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16


class ControlServer:
    def __init__(
        self,
        host: str,
        port: int,
        max_request_provider: Callable[[], int],
        dispatcher: Callable[[str, dict[str, Any]], Any],
    ) -> None:
        self._host = host
        self._port = port
        self._max_request_provider = max_request_provider
        self._dispatcher = dispatcher
        self._server: _ThreadingTcpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server:
            return
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                max_bytes = owner._max_request_provider()
                while True:
                    line = self.rfile.readline(max_bytes + 1)
                    if not line:
                        return
                    if len(line) > max_bytes:
                        self._send({"ok": False, "error": "request_too_large"})
                        return
                    try:
                        request = json.loads(line.decode("utf-8"))
                        response = owner._handle_request(request)
                    except json.JSONDecodeError as exc:
                        response = {"ok": False, "error": "invalid_json", "message": str(exc)}
                    except UnicodeDecodeError as exc:
                        response = {"ok": False, "error": "invalid_utf8", "message": str(exc)}
                    except Exception as exc:
                        LOGGER.exception("Control request failed")
                        response = {"ok": False, "error": "internal_error", "message": str(exc)}
                    self._send(response)

            def _send(self, response: dict[str, Any]) -> None:
                encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                self.wfile.write(encoded)
                self.wfile.flush()

        server = _ThreadingTcpServer((self._host, self._port), Handler)
        try:
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.25},
                name="xgt-control-server",
                daemon=True,
            )
            thread.start()
        except Exception:
            server.server_close()
            raise
        self._server = server
        self._thread = thread
        LOGGER.info("Control server listening on %s:%s", *self.address)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None

    def _handle_request(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "invalid_request", "message": "JSON object required"}
        request_id = request.get("id")
        command = request.get("command")
        params = request.get("params", {})
        if not isinstance(command, str) or not isinstance(params, dict):
            return {
                "id": request_id,
                "ok": False,
                "error": "invalid_request",
                "message": "command must be a string and params must be an object",
            }
        try:
            result = self._dispatcher(command, params)
            return {"id": request_id, "ok": True, "result": result}
        except (ValueError, KeyError, TypeError) as exc:
            return {"id": request_id, "ok": False, "error": "bad_request", "message": str(exc)}
        except Exception as exc:
            LOGGER.exception("Command %s failed", command)
            return {"id": request_id, "ok": False, "error": "command_failed", "message": str(exc)}
