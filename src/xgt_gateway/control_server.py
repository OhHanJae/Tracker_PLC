"""Newline-delimited JSON TCP control server."""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


class _ThreadingTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._client_slots = threading.BoundedSemaphore(16)
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._client_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._clients_lock:
            self._clients.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._clients_lock:
                self._clients.discard(request)
            self._client_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._clients_lock:
                self._clients.discard(request)
            self._client_slots.release()

    def server_close(self) -> None:
        with self._clients_lock:
            clients = tuple(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        super().server_close()


class ControlServer:
    def __init__(
        self,
        host: str,
        port: int,
        max_request_provider: Callable[[], int],
        dispatcher: Callable[[str, dict[str, Any]], Any],
        *,
        io_timeout_s: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._max_request_provider = max_request_provider
        self._dispatcher = dispatcher
        self._io_timeout_s = io_timeout_s
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
                self.connection.settimeout(owner._io_timeout_s)
                try:
                    self._serve()
                except (OSError, ValueError):
                    return

            def _serve(self) -> None:
                max_bytes = owner._max_request_provider()
                while True:
                    line = self.rfile.readline(max_bytes + 1)
                    if not line:
                        return
                    if len(line) > max_bytes:
                        self._send({"ok": False, "error": "request_too_large"})
                        return
                    if not line.endswith(b"\n"):
                        self._send({"ok": False, "error": "incomplete_frame"})
                        return
                    try:
                        request = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
                        response = owner._handle_request(request)
                    except UnicodeDecodeError as exc:
                        response = {"ok": False, "error": "invalid_utf8", "message": str(exc)}
                    except ValueError as exc:
                        response = {"ok": False, "error": "invalid_json", "message": str(exc)}
                    except Exception as exc:
                        LOGGER.exception("Control request failed")
                        response = {"ok": False, "error": "internal_error", "message": str(exc)}
                    self._send(response)

            def _send(self, response: dict[str, Any]) -> None:
                encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
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
