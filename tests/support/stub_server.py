"""Loopback HTTP stub server for transport and CLI tests."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

ResponseMode = Literal["normal", "close", "malformed", "hang"]


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    json: Any


@dataclass
class StubResponse:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    mode: ResponseMode = "normal"


def json_response(obj: Any, status: int = 200) -> StubResponse:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return StubResponse(
        status=status,
        body=body,
        headers={"Content-Type": "application/json"},
    )


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            server.body_reads += 1  # type: ignore[attr-defined]
            body = self.rfile.read(length)
        else:
            body = b""
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                parsed = None
        request = RecordedRequest(
            method=self.command,
            path=self.path,
            headers={name: value for name, value in self.headers.items()},
            body=body,
            json=parsed,
        )
        server.record(request)  # type: ignore[attr-defined]
        self._respond(server.responder(request))  # type: ignore[attr-defined]

    def _respond(self, response: StubResponse) -> None:
        if response.mode == "close":
            self.close_connection = True
            return
        if response.mode == "malformed":
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 64\r\n\r\n"
                b'{"broken": '
            )
            self.wfile.flush()
            self.close_connection = True
            return
        if response.mode == "hang":
            time.sleep(1.0)
            self.close_connection = True
            return
        self.send_response(response.status)
        headers = dict(response.headers)
        headers.setdefault("Content-Type", "application/json")
        for name, value in headers.items():
            self.send_header(name, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)
        self.wfile.flush()


class _StubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        responder: Callable[[RecordedRequest], StubResponse],
    ) -> None:
        self.responder = responder
        self.requests: list[RecordedRequest] = []
        self.body_reads = 0
        super().__init__(server_address, _StubHandler)

    def record(self, request: RecordedRequest) -> None:
        self.requests.append(request)

    def server_bind(self) -> None:
        # Skip http.server's reverse-DNS lookup so the stub stays offline.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class _StubHTTPServerV6(_StubHTTPServer):
    address_family = socket.AF_INET6


class StubServer:
    """Context-managed loopback HTTP server capturing every request."""

    def __init__(
        self,
        responder: Callable[[RecordedRequest], StubResponse],
        ipv6: bool = False,
    ) -> None:
        self.ipv6 = ipv6
        server_class: type[_StubHTTPServer] = _StubHTTPServerV6 if ipv6 else _StubHTTPServer
        host = "::1" if ipv6 else "127.0.0.1"
        self._httpd = server_class((host, 0), responder)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host = self._httpd.server_address[0]
        port = self._httpd.server_address[1]
        display = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{display}:{port}"

    @property
    def requests(self) -> list[RecordedRequest]:
        return self._httpd.requests

    @property
    def body_reads(self) -> int:
        return self._httpd.body_reads

    @property
    def server_address(self) -> tuple[str, int]:
        address = self._httpd.server_address
        return str(address[0]), int(address[1])

    def __enter__(self) -> StubServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False
