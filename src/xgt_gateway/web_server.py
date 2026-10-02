"""Dependency-free HTTP configuration server."""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 16


class WebServer:
    def __init__(
        self,
        host: str,
        port: int,
        api_dispatcher: Callable[[str, str, dict[str, Any]], Any],
    ) -> None:
        self._host = host
        self._port = port
        self._api_dispatcher = api_dispatcher
        self._server: _HttpServer | None = None
        self._thread: threading.Thread | None = None
        self._html = (Path(__file__).with_name("web") / "index.html").read_bytes()

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

        class Handler(BaseHTTPRequestHandler):
            server_version = "XGTGateway/1.0"

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/":
                    self._send_bytes(HTTPStatus.OK, owner._html, "text/html; charset=utf-8")
                    return
                if path == "/favicon.ico":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._handle_api("GET", path)

            def do_POST(self) -> None:  # noqa: N802
                self._handle_api("POST", urlparse(self.path).path)

            def do_PUT(self) -> None:  # noqa: N802
                self._handle_api("PUT", urlparse(self.path).path)

            def _handle_api(self, method: str, path: str) -> None:
                if not path.startswith("/api/"):
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                    return
                try:
                    body = self._read_json_body() if method in {"POST", "PUT"} else {}
                    result = owner._api_dispatcher(method, path, body)
                    self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
                except KeyError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found", "message": str(exc)})
                except (ValueError, TypeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad_request", "message": str(exc)})
                except Exception as exc:
                    LOGGER.exception("HTTP API request failed")
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": "internal_error", "message": str(exc)},
                    )

            def _read_json_body(self) -> dict[str, Any]:
                length_text = self.headers.get("Content-Length", "0")
                try:
                    length = int(length_text)
                except ValueError as exc:
                    raise ValueError("Invalid Content-Length") from exc
                if not 0 <= length <= 1_048_576:
                    raise ValueError("Request body is too large")
                if length == 0:
                    return {}
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("JSON request body must be an object")
                return value

            def _send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
                data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._send_bytes(status, data, "application/json; charset=utf-8")

            def _send_bytes(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                LOGGER.debug("HTTP %s - %s", self.client_address[0], fmt % args)

        server = _HttpServer((self._host, self._port), Handler)
        try:
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.25},
                name="xgt-web-server",
                daemon=True,
            )
            thread.start()
        except Exception:
            server.server_close()
            raise
        self._server = server
        self._thread = thread
        LOGGER.info("Web server listening on %s:%s", *self.address)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None
