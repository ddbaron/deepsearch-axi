"""Self-tests for the offline socket guard and the loopback stub harness."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from support import netguard
from support.stub_server import json_response

BLOCKED_ADDRESS = ("192.0.2.1", 80)


def test_parent_blocks_non_loopback_connect() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(netguard.NetworkBlocked):
            sock.connect(BLOCKED_ADDRESS)
    finally:
        sock.close()


def test_parent_blocks_non_loopback_connect_ex() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(netguard.NetworkBlocked):
            sock.connect_ex(BLOCKED_ADDRESS)
    finally:
        sock.close()


def test_parent_blocks_non_loopback_dns() -> None:
    with pytest.raises(netguard.NetworkBlocked):
        socket.getaddrinfo("example.com", 443)


def test_child_python_blocks_non_loopback_connect(env: dict[str, str], tmp_path: Path) -> None:
    code = "import socket; socket.socket().connect(('192.0.2.1', 80))"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert b"NetworkBlocked" in completed.stderr


def test_child_python_allows_loopback_connect(env: dict[str, str], tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        code = (
            "import socket; "
            f"s = socket.create_connection(('127.0.0.1', {port}), 5); "
            "s.close(); print('ok')"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
    finally:
        listener.close()
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b"ok\n"


def test_stub_server_captures_request(stub) -> None:
    server = stub(lambda request: json_response({"ok": True}))
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(
            "POST",
            "/v1/tools/search",
            body=b'{"query": "asyncio"}',
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer sk-test-FAKEKEY",
            },
        )
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    assert response.status == 200
    assert json.loads(body) == {"ok": True}
    assert len(server.requests) == 1
    recorded = server.requests[0]
    assert recorded.method == "POST"
    assert recorded.path == "/v1/tools/search"
    assert recorded.json == {"query": "asyncio"}
    assert recorded.headers["Authorization"] == "Bearer sk-test-FAKEKEY"
    assert server.body_reads == 1
