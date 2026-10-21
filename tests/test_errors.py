"""Error-surface acceptance tests: status mapping, transport failures, 200 schema.

These tests drive the real transport against the loopback stub.  Response
bodies are observed through ``HTTPResponse`` read instrumentation, so "non-200
bodies are never consumed" is proven rather than inferred.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest
from support.cli import decode, run_cli
from support.env import FAKE_KEY, isolated_env
from support.stub_server import StubResponse, json_response

from deepsearch_axi import transport
from deepsearch_axi.failures import status_failure
from deepsearch_axi.models import (
    EXIT_FAILURE,
    MAX_RESPONSE_BYTES,
    SEARCH_FIELDS,
    TIMEOUT_SECONDS,
    Credential,
    DeepsearchError,
    Failure,
    HttpReply,
    SearchOptions,
    exit_code_for,
)
from deepsearch_axi.responses import validate_fetch_response, validate_search_response

_STATUS_CASES = [
    (199, "http_error"),
    (201, "http_error"),
    (204, "http_error"),
    (300, "redirect_refused"),
    (301, "redirect_refused"),
    (302, "redirect_refused"),
    (303, "redirect_refused"),
    (307, "redirect_refused"),
    (308, "redirect_refused"),
    (400, "invalid_request"),
    (401, "unauthorized"),
    (402, "insufficient_balance"),
    (403, "forbidden"),
    (404, "not_found"),
    (408, "timeout"),
    (413, "request_too_large"),
    (418, "http_error"),
    (422, "http_error"),
    (429, "rate_limited"),
    (500, "upstream_error"),
    (503, "upstream_error"),
    (504, "timeout"),
]


def _credential() -> Credential:
    return Credential(value=FAKE_KEY, source="env")


def _assert_safe_failure(failure: Failure, code: str, status: int | None) -> None:
    assert failure.code == code
    assert failure.http_status == status
    assert 1 <= len(failure.help) <= 2
    assert exit_code_for(failure) == EXIT_FAILURE
    for text in (failure.message, *failure.help):
        assert FAKE_KEY not in text


def _assert_no_raw_exception(exc: DeepsearchError, needles: tuple[str, ...]) -> None:
    visible = f"{exc!r} {exc} {exc.failure.message} {' '.join(exc.failure.help)}"
    for needle in needles:
        assert needle not in visible
    assert exc.args == (exc.failure.code,)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


class ResponseReadSpy:
    """Record every call to the response body readers."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._read = http.client.HTTPResponse.read
        self._read1 = http.client.HTTPResponse.read1
        self._readinto = http.client.HTTPResponse.readinto

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read = self._read
        read1 = self._read1
        readinto = self._readinto

        def spy_read(response: http.client.HTTPResponse, amt: int | None = None) -> bytes:
            self.calls.append("read")
            return read(response, amt)

        def spy_read1(response: http.client.HTTPResponse, amt: int = -1) -> bytes:
            self.calls.append("read1")
            return read1(response, amt)

        def spy_readinto(response: http.client.HTTPResponse, buffer: Any) -> int:
            self.calls.append("readinto")
            return readinto(response, buffer)

        monkeypatch.setattr(http.client.HTTPResponse, "read", spy_read)
        monkeypatch.setattr(http.client.HTTPResponse, "read1", spy_read1)
        monkeypatch.setattr(http.client.HTTPResponse, "readinto", spy_readinto)


class CountingReader:
    """Count every body byte handed to the transport's bounded reader."""

    def __init__(self, fp: Any) -> None:
        self._fp = fp
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self._fp.read(size)
        self.bytes_read += len(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        data = self._fp.readline(size)
        self.bytes_read += len(data)
        return data

    def close(self) -> None:
        self._fp.close()

    def flush(self) -> None:
        self._fp.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fp, name)


@pytest.fixture
def response_read_counter(monkeypatch: pytest.MonkeyPatch) -> list[CountingReader]:
    readers: list[CountingReader] = []
    real_begin = http.client.HTTPResponse.begin

    def begin(response: http.client.HTTPResponse) -> None:
        real_begin(response)
        if response.fp is not None:
            response.fp = CountingReader(response.fp)
            readers.append(response.fp)

    monkeypatch.setattr(http.client.HTTPResponse, "begin", begin)
    return readers


_RAW_CHUNKED_HEADERS = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
)


def _chunked_wire(body: bytes, chunk_size: int = 65536) -> bytes:
    pieces = [body[offset : offset + chunk_size] for offset in range(0, len(body), chunk_size)]
    framing = b"".join(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n" for piece in pieces)
    return framing + b"0\r\n\r\n"


def _json_body_of_size(size: int) -> bytes:
    prefix = b'{"pad":"'
    suffix = b'"}'
    assert size >= len(prefix) + len(suffix)
    return prefix + b"a" * (size - len(prefix) - len(suffix)) + suffix


class RawChunkedServer:
    """Loopback server writing exact (including malformed) chunked framing.

    ``http.server`` always emits well-formed chunk sizes, so the hostile
    framing cases need a raw socket.  The body after a rejected size line is
    large enough that a whole-stream read is visible in the client-side count.
    """

    def __init__(self, wire_body: bytes) -> None:
        self._wire_body = wire_body
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.1)
        self.requests: list[bytes] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                request = bytearray()
                try:
                    while b"\r\n\r\n" not in request:
                        piece = connection.recv(65536)
                        if not piece:
                            break
                        request.extend(piece)
                    headers, _, body = request.partition(b"\r\n\r\n")
                    length = 0
                    for line in headers.split(b"\r\n")[1:]:
                        name, _, value = line.partition(b":")
                        if name.strip().lower() == b"content-length":
                            length = int(value.strip() or b"0")
                    while len(body) < length:
                        piece = connection.recv(65536)
                        if not piece:
                            break
                        body += piece
                    self.requests.append(bytes(headers + b"\r\n\r\n" + body))
                    # Draining the request body before close avoids a reset
                    # that would discard the tail of the response on loopback.
                    connection.sendall(_RAW_CHUNKED_HEADERS + self._wire_body)
                except OSError:
                    pass

    def __enter__(self) -> RawChunkedServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self._listener.close()
        self._thread.join(timeout=5)
        return False


@pytest.mark.parametrize(
    ("status", "code"),
    _STATUS_CASES,
    ids=[str(status) for status, _ in _STATUS_CASES],
)
def test_http_status_mapping(
    status: int,
    code: str,
    stub: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = f"reflected-body-{status}"
    body = json.dumps({"error": marker, "key": FAKE_KEY}).encode("utf-8")

    def responder(request: Any) -> StubResponse:
        return StubResponse(
            status=status,
            body=body,
            headers={"Content-Type": "application/json", "Location": "https://elsewhere.invalid/x"},
        )

    server = stub(responder)
    spy = ResponseReadSpy()
    spy.install(monkeypatch)

    reply = transport.post_tool(server.base_url, "search", {"query": "status probe"}, _credential())

    assert reply == HttpReply(status=status, payload=None)
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.method == "POST"
    assert request.path == "/v1/tools/search"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request.json == {"query": "status probe"}

    failure = status_failure(reply.status, "search")
    _assert_safe_failure(failure, code, status)
    for text in (failure.message, *failure.help):
        assert marker not in text
        assert "elsewhere.invalid" not in text
    assert spy.calls == []
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


_FETCH_STATUS_CASES = [
    (400, "invalid_request"),
    (401, "unauthorized"),
    (402, "insufficient_balance"),
    (403, "forbidden"),
    (404, "not_found"),
    (408, "timeout"),
    (413, "request_too_large"),
    (429, "rate_limited"),
    (500, "upstream_error"),
    (504, "timeout"),
]


@pytest.mark.parametrize(
    ("status", "code"),
    _FETCH_STATUS_CASES,
    ids=[str(status) for status, _ in _FETCH_STATUS_CASES],
)
def test_fetch_documented_statuses_map_without_body_reads(
    status: int,
    code: str,
    stub: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = f"fetch-reflected-{status}"
    body = json.dumps({"error": marker, "key": FAKE_KEY}).encode("utf-8")
    server = stub(
        lambda request: StubResponse(
            status=status,
            body=body,
            headers={"Location": "https://elsewhere.invalid/fetch"},
        )
    )
    spy = ResponseReadSpy()
    spy.install(monkeypatch)

    reply = transport.post_tool(
        server.base_url, "fetch", {"url": "https://example.com/page"}, _credential()
    )

    assert reply == HttpReply(status=status, payload=None)
    assert len(server.requests) == 1
    assert server.requests[0].path == "/v1/tools/fetch"
    assert server.requests[0].json == {"url": "https://example.com/page"}
    failure = status_failure(reply.status, "fetch")
    _assert_safe_failure(failure, code, status)
    assert any("deepsearch-axi fetch" in hint for hint in failure.help)
    assert not any("deepsearch-axi search" in hint for hint in failure.help)
    for text in (failure.message, *failure.help):
        assert marker not in text
        assert "elsewhere.invalid" not in text
    assert spy.calls == []


def test_non_http_reply_is_transport_error() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted: list[bytes] = []

    def serve_garbage() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            accepted.append(connection.recv(65536))
            connection.sendall(b"GARBAGE NOT HTTP\r\n\r\n")

    thread = threading.Thread(target=serve_garbage, daemon=True)
    thread.start()
    try:
        with pytest.raises(DeepsearchError) as garbage:
            transport.post_tool(
                f"http://127.0.0.1:{port}",
                "search",
                {"query": "q"},
                _credential(),
                timeout_seconds=5,
            )
    finally:
        listener.close()
        thread.join(timeout=5)

    _assert_safe_failure(garbage.value.failure, "transport_error", None)
    _assert_no_raw_exception(garbage.value, ("BadStatusLine", "GARBAGE", "Traceback"))
    assert len(accepted) == 1
    assert accepted[0].startswith(b"POST /v1/tools/search ")


_OVERFLOW_BODIES = [
    pytest.param(b'{"results":[],"unknown":1e999}', id="unknown-field"),
    pytest.param(b'{"results":[{"title":"t","rerank_score":1e999}]}', id="known-numeric-field"),
    pytest.param(b'{"results":[],"unknown":-1e999}', id="negative-overflow"),
]


@pytest.mark.parametrize("body", _OVERFLOW_BODIES)
def test_overflowing_json_numbers_are_invalid_response(
    body: bytes,
    stub: Any,
) -> None:
    server = stub(lambda request: StubResponse(status=200, body=body))

    with pytest.raises(DeepsearchError) as overflow:
        transport.post_tool(server.base_url, "search", {"query": "q"}, _credential())

    _assert_safe_failure(overflow.value.failure, "invalid_response", None)
    _assert_no_raw_exception(overflow.value, ("1e999", "inf", "Traceback"))
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("size_line", "code"),
    [(b"-1", "transport_error"), (b"90000a", "response_too_large")],
    ids=["negative-size", "oversized-size"],
)
def test_chunked_malformed_size_is_rejected_before_body_read(
    size_line: bytes,
    code: str,
    response_read_counter: list[CountingReader],
) -> None:
    # A CPython HTTPResponse.read() delegates the wire chunk size straight to
    # _safe_read(), so "-1" consumes the whole remaining stream.  The transport
    # must validate framing first and only consume the size line.
    filler = b"a" * (256 * 1024)
    wire_body = size_line + b"\r\n" + filler + b"\r\n0\r\n\r\n"
    with RawChunkedServer(wire_body) as server:
        with pytest.raises(DeepsearchError) as failure:
            transport.post_tool(
                server.base_url, "search", {"query": "q"}, _credential(), timeout_seconds=5
            )
    assert failure.value.failure.code == code
    assert failure.value.failure.http_status is None
    _assert_no_raw_exception(failure.value, ("IncompleteRead", "ValueError", "Traceback"))
    assert len(server.requests) == 1
    assert server.requests[0].startswith(b"POST /v1/tools/search ")
    assert sum(reader.bytes_read for reader in response_read_counter) <= 64


def test_chunked_response_at_exact_limit_succeeds(
    response_read_counter: list[CountingReader],
) -> None:
    body = _json_body_of_size(MAX_RESPONSE_BYTES)
    with RawChunkedServer(_chunked_wire(body)) as server:
        reply = transport.post_tool(
            server.base_url, "search", {"query": "q"}, _credential(), timeout_seconds=10
        )
    assert reply.status == 200
    assert isinstance(reply.payload, dict)
    assert len(reply.payload["pad"]) == MAX_RESPONSE_BYTES - len(b'{"pad":"') - len(b'"}')
    assert len(server.requests) == 1
    # Framing overhead is ~17 bytes per 64 KiB chunk; the point is that the
    # reader never runs away past the cap.
    assert sum(reader.bytes_read for reader in response_read_counter) <= MAX_RESPONSE_BYTES + 8192


def test_chunked_response_over_limit_fails_without_whole_body_read(
    response_read_counter: list[CountingReader],
) -> None:
    body = _json_body_of_size(MAX_RESPONSE_BYTES + 1)
    with RawChunkedServer(_chunked_wire(body)) as server:
        with pytest.raises(DeepsearchError) as too_large:
            transport.post_tool(
                server.base_url, "search", {"query": "q"}, _credential(), timeout_seconds=10
            )
    _assert_safe_failure(too_large.value.failure, "response_too_large", None)
    assert len(server.requests) == 1
    assert sum(reader.bytes_read for reader in response_read_counter) <= MAX_RESPONSE_BYTES + 8192


@pytest.mark.parametrize(
    "credential_value",
    ["bad\r\nX-Injected: 1", "caf\u00e9-\u4e2d"],
    ids=["header-control", "non-latin1"],
)
def test_unencodable_credential_fails_as_safe_failure(credential_value: str, stub: Any) -> None:
    # Dependency-level header encoding errors must not escape as raw exceptions
    # whose messages echo the bearer value.
    server = stub(lambda request: json_response({"results": []}))
    credential = Credential(value=credential_value, source="env")

    with pytest.raises(DeepsearchError) as unsafe:
        transport.post_tool(server.base_url, "search", {"query": "q"}, credential)

    assert unsafe.value.failure.code in {"invalid_config", "transport_error"}
    assert unsafe.value.failure.http_status is None
    _assert_no_raw_exception(
        unsafe.value,
        ("Invalid header", "latin-1", "codec", "X-Injected", "Bearer", "Traceback"),
    )
    assert server.requests == []


@pytest.mark.parametrize(
    ("base_url", "code"),
    [
        pytest.param("https://" + "e" * 64 + ".invalid", "transport_error", id="long-label"),
        pytest.param("https://\ud800.invalid", "invalid_config", id="surrogate"),
    ],
)
def test_invalid_hostname_fails_as_safe_failure(
    base_url: str, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The session netguard answers socket.create_connection before the platform
    # IDNA encoder runs; mirror that encoder here and prove the UnicodeError is
    # mapped while no socket is ever opened. A validator-rejected surrogate
    # origin is refused even earlier as invalid_config.
    def encode_then_block(address: tuple[str, int], *args: Any, **kwargs: Any) -> None:
        address[0].encode("idna")
        raise AssertionError("external network access is blocked")

    monkeypatch.setattr(socket, "create_connection", encode_then_block)

    with pytest.raises(DeepsearchError) as unsafe:
        transport.post_tool(base_url, "search", {"query": "q"}, _credential(), timeout_seconds=1)

    _assert_safe_failure(unsafe.value.failure, code, None)
    _assert_no_raw_exception(
        unsafe.value,
        ("label empty or too long", "Invalid character", "Unicode", "Traceback"),
    )


def test_transport_failures(stub: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    credential = _credential()
    recorded: dict[str, float | None] = {}
    real_connection = http.client.HTTPConnection

    class RecordingConnection(real_connection):
        def __init__(
            self, host: str, port: int | None = None, timeout: float | None = None, **kwargs: Any
        ) -> None:
            recorded["timeout"] = timeout
            super().__init__(host, port, timeout=timeout, **kwargs)

    ok_server = stub(lambda request: json_response({"results": []}))
    monkeypatch.setattr(http.client, "HTTPConnection", RecordingConnection)
    reply = transport.post_tool(ok_server.base_url, "search", {"query": "q"}, credential)
    assert reply.status == 200
    assert recorded["timeout"] == 30.0
    assert TIMEOUT_SECONDS == 30.0

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    refused_port = holder.getsockname()[1]
    holder.close()
    with pytest.raises(DeepsearchError) as refused:
        transport.post_tool(
            f"http://127.0.0.1:{refused_port}", "search", {"query": "q"}, credential
        )
    _assert_safe_failure(refused.value.failure, "transport_error", None)
    _assert_no_raw_exception(refused.value, ("Connection refused", "Errno", "Traceback"))

    close_server = stub(lambda request: StubResponse(mode="close"))
    with pytest.raises(DeepsearchError) as closed:
        transport.post_tool(close_server.base_url, "search", {"query": "q"}, credential)
    _assert_safe_failure(closed.value.failure, "transport_error", None)
    _assert_no_raw_exception(closed.value, ("RemoteDisconnected", "Traceback"))
    assert len(close_server.requests) == 1

    malformed_server = stub(lambda request: StubResponse(mode="malformed"))
    with pytest.raises(DeepsearchError) as malformed:
        transport.post_tool(malformed_server.base_url, "search", {"query": "q"}, credential)
    # A connection that dies mid-response may surface as bad framing
    # (transport_error) or truncated JSON (invalid_response); both are safe.
    assert malformed.value.failure.code in {"transport_error", "invalid_response"}
    assert malformed.value.failure.http_status is None
    _assert_no_raw_exception(malformed.value, ("IncompleteRead", "JSONDecodeError", "Traceback"))
    assert len(malformed_server.requests) == 1

    hang_server = stub(lambda request: StubResponse(mode="hang"))
    with pytest.raises(DeepsearchError) as timed_out:
        transport.post_tool(
            hang_server.base_url, "search", {"query": "q"}, credential, timeout_seconds=0.05
        )
    _assert_safe_failure(timed_out.value.failure, "timeout", None)
    _assert_no_raw_exception(timed_out.value, ("TimeoutError", "socket.timeout", "Traceback"))
    assert len(hang_server.requests) == 1

    def refuse_dns(connection: http.client.HTTPSConnection) -> None:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse_dns)
    with pytest.raises(DeepsearchError) as dns_failure:
        transport.post_tool("https://search.invalid", "search", {"query": "q"}, credential)
    _assert_safe_failure(dns_failure.value.failure, "transport_error", None)
    _assert_no_raw_exception(dns_failure.value, ("Name or service not known", "gaierror"))
    monkeypatch.undo()

    tls_server = stub(lambda request: json_response({}))
    with pytest.raises(DeepsearchError) as tls_failure:
        transport.post_tool(
            f"https://127.0.0.1:{tls_server.server_address[1]}",
            "search",
            {"query": "q"},
            credential,
        )
    _assert_safe_failure(tls_failure.value.failure, "transport_error", None)
    _assert_no_raw_exception(tls_failure.value, ("SSL", "WRONG_VERSION_NUMBER", "Traceback"))
    # The plaintext server never saw an HTTP request, let alone the key.
    assert tls_server.requests == []


def _search_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": "Title",
        "url": "https://example.com/",
        "snippet": "Snippet",
        "summary": "Summary",
        "content": "Body",
        "date": None,
        "authority": 2,
        "rerank_score": 1.5,
        "site_name": None,
        "site_icon": None,
        "unknown_field": "must-not-leak",
    }
    item.update(overrides)
    return item


def _without(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in keys}


def test_response_limits_and_schema(stub: Any, tmp_path: Path) -> None:
    credential = _credential()

    prefix = b'{"pad":"'
    suffix = b'"}'
    at_limit_body = prefix + b"a" * (MAX_RESPONSE_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(at_limit_body) == MAX_RESPONSE_BYTES
    at_limit = stub(lambda request: StubResponse(status=200, body=at_limit_body))
    reply = transport.post_tool(at_limit.base_url, "search", {"query": "q"}, credential)
    assert reply.status == 200
    assert isinstance(reply.payload, dict)
    assert len(reply.payload["pad"]) == MAX_RESPONSE_BYTES - len(prefix) - len(suffix)
    assert len(at_limit.requests) == 1

    over_limit = stub(lambda request: StubResponse(status=200, body=at_limit_body[:-2] + b'x"}'))
    with pytest.raises(DeepsearchError) as too_large:
        transport.post_tool(over_limit.base_url, "search", {"query": "q"}, credential)
    _assert_safe_failure(too_large.value.failure, "response_too_large", None)
    assert len(over_limit.requests) == 1

    bad_bodies = [
        ("truncated json", b'{"broken": '),
        ("plain text", b"not json"),
        ("invalid utf8", b'{"title": "\xff"}'),
        ("nan", b'{"results": NaN}'),
        ("infinity", b'{"results": Infinity}'),
        ("negative infinity", b'{"results": -Infinity}'),
        ("array root", b"[]"),
        ("string root", b'"root"'),
        ("null root", b"null"),
        ("number root", b"42"),
    ]
    for label, bad_body in bad_bodies:
        bad_server = stub(
            lambda request, bad_body=bad_body: StubResponse(status=200, body=bad_body)
        )
        with pytest.raises(DeepsearchError) as invalid:
            transport.post_tool(bad_server.base_url, "search", {"query": "q"}, credential)
        assert invalid.value.failure.code == "invalid_response", label
        assert invalid.value.failure.http_status is None, label
        assert len(bad_server.requests) == 1, label

    valid_item = _search_item()
    invalid_payloads: list[tuple[str, Any, int]] = [
        ("missing results", {}, 1),
        ("results not a list", {"results": {}}, 1),
        ("over-returned rows", {"results": [valid_item, _search_item()]}, 1),
        ("row not an object", {"results": ["row"]}, 1),
        ("missing title", {"results": [_without(valid_item, "title")]}, 1),
        ("missing url", {"results": [_without(valid_item, "url")]}, 1),
        ("missing snippet", {"results": [_without(valid_item, "snippet")]}, 1),
        ("missing summary", {"results": [_without(valid_item, "summary")]}, 1),
        ("missing content", {"results": [_without(valid_item, "content")]}, 1),
        ("missing authority", {"results": [_without(valid_item, "authority")]}, 1),
        ("missing rerank_score", {"results": [_without(valid_item, "rerank_score")]}, 1),
        ("title wrong type", {"results": [_search_item(title=3)]}, 1),
        ("url null", {"results": [_search_item(url=None)]}, 1),
        ("snippet list", {"results": [_search_item(snippet=[])]}, 1),
        ("summary object", {"results": [_search_item(summary={})]}, 1),
        ("content number", {"results": [_search_item(content=7)]}, 1),
        ("date number", {"results": [_search_item(date=1)]}, 1),
        ("site_name bool", {"results": [_search_item(site_name=True)]}, 1),
        ("site_icon float", {"results": [_search_item(site_icon=1.2)]}, 1),
        ("authority bool", {"results": [_search_item(authority=True)]}, 1),
        ("authority text", {"results": [_search_item(authority="2")]}, 1),
        ("authority below range", {"results": [_search_item(authority=-2)]}, 1),
        ("authority above range", {"results": [_search_item(authority=5)]}, 1),
        ("rerank bool", {"results": [_search_item(rerank_score=False)]}, 1),
        ("rerank text", {"results": [_search_item(rerank_score="1")]}, 1),
        ("rerank below range", {"results": [_search_item(rerank_score=-0.1)]}, 1),
        ("rerank above range", {"results": [_search_item(rerank_score=3.1)]}, 1),
        ("rerank nan", {"results": [_search_item(rerank_score=float("nan"))]}, 1),
        ("rerank inf", {"results": [_search_item(rerank_score=float("inf"))]}, 1),
    ]
    for label, payload, requested_count in invalid_payloads:
        with pytest.raises(DeepsearchError) as invalid:
            validate_search_response(payload, requested_count)
        assert invalid.value.failure.code == "invalid_response", label
        assert invalid.value.failure.http_status is None, label

    assert validate_search_response({"results": []}, 0) == []
    for authority in (-1, 4):
        rows = validate_search_response({"results": [_search_item(authority=authority)]}, 1)
        assert rows[0]["authority"] == authority
    for score in (0, 3, 0.0, 3.0, 1.5):
        rows = validate_search_response({"results": [_search_item(rerank_score=score)]}, 1)
        assert rows[0]["rerank_score"] == score

    rows = validate_search_response({"results": [valid_item]}, 1)
    assert list(rows[0]) == list(SEARCH_FIELDS)
    assert "unknown_field" not in rows[0]
    assert rows[0]["date"] is None
    assert rows[0]["site_name"] is None
    assert rows[0]["site_icon"] is None

    stripped = _without(valid_item, "date", "site_name", "site_icon")
    projected = validate_search_response({"results": [stripped]}, 1)[0]
    assert projected["date"] is None
    assert projected["site_name"] is None
    assert projected["site_icon"] is None

    invalid_pages: list[tuple[str, Any]] = [
        ("missing title", {"content": "Body"}),
        ("missing content", {"title": "Title"}),
        ("title wrong type", {"title": 1, "content": "Body"}),
        ("content null", {"title": "Title", "content": None}),
        ("blank content", {"title": "Title", "content": " \n\t"}),
        ("root not an object", []),
    ]
    for label, page in invalid_pages:
        with pytest.raises(DeepsearchError) as invalid:
            validate_fetch_response(page)
        assert invalid.value.failure.code == "invalid_response", label
        assert invalid.value.failure.http_status is None, label

    page = validate_fetch_response({"title": "Title", "content": "Body", "unknown": "x"})
    assert page == {"title": "Title", "content": "Body"}
    assert list(page) == ["title", "content"]

    # --- AC-20: escaped lone surrogates never become a successful response ---
    project = tmp_path / "project"
    project.mkdir()

    def run_with(body: bytes, args: list[str]) -> tuple[Any, Any]:
        response_server = stub(lambda request, body=body: StubResponse(status=200, body=body))
        run_env = isolated_env(
            tmp_path / "cli-home",
            DEEPSEEK_API_KEY=FAKE_KEY,
            DEEPSEEK_BASE_URL=response_server.base_url,
        )
        return response_server, run_cli(args, env=run_env, cwd=project)

    invalid_searches = [
        ("selected-title-high", {"results": [_search_item(title="bad\ud800")]}),
        ("selected-content-low", {"results": [_search_item(content="bad\udfff")]}),
        ("unselected-date-high", {"results": [_search_item(date="bad\ud800")]}),
        ("unknown-top-level", {"results": [_search_item()], "unknown": "bad\ud800"}),
        (
            "unknown-nested",
            {"results": [_search_item()], "meta": {"deep": ["bad\udfff"]}},
        ),
        ("unknown-object-key", {"results": [_search_item()], "\ud800": "x"}),
        ("nested-object-key", {"results": [_search_item()], "meta": {"\udfff": 1}}),
    ]
    for label, payload in invalid_searches:
        body = json.dumps(payload).encode("ascii")
        assert b"\\ud" in body, label
        response_server, run = run_with(body, ["search", "unicode probe"])
        assert run.code == 1, label
        assert run.stderr == "", label
        document = decode(run.stdout)
        assert document["error"]["code"] == "invalid_response", label
        # A successful repair would exit 0 and substitute U+FFFD; neither is
        # acceptable for invalid API data.
        assert "\ufffd" not in run.stdout, label
        assert FAKE_KEY not in run.stdout + run.stderr, label
        assert len(response_server.requests) == 1, label

    invalid_pages = [
        ("fetch-title-high", {"title": "bad\ud800", "content": "Body"}),
        ("fetch-content-low", {"title": "Title", "content": "bad\udfff"}),
        (
            "fetch-unknown-nested",
            {"title": "Title", "content": "Body", "meta": {"x": ["\ud800"]}},
        ),
        ("fetch-object-key", {"title": "Title", "content": "Body", "\ud800": "x"}),
    ]
    for label, payload in invalid_pages:
        body = json.dumps(payload).encode("ascii")
        response_server, run = run_with(body, ["fetch", "https://example.com/"])
        assert run.code == 1, label
        assert run.stderr == "", label
        document = decode(run.stdout)
        assert document["error"]["code"] == "invalid_response", label
        assert "\ufffd" not in run.stdout, label
        assert FAKE_KEY not in run.stdout + run.stderr, label
        assert len(response_server.requests) == 1, label

    valid_search = json.dumps(
        {"results": [_search_item(title="🌍 earth", content="café ☕", snippet="🌍")]},
        ensure_ascii=False,
    ).encode("utf-8")
    response_server, run = run_with(valid_search, ["search", "unicode probe", "--need-content"])
    assert run.code == 0, run.stdout + run.stderr
    document = decode(run.stdout)
    assert document["results"][0]["title"] == "🌍 earth"
    assert document["results"][0]["content"] == "café ☕"

    # A valid escaped surrogate pair is one supplementary-plane scalar and is
    # preserved rather than rejected.
    escaped_pair = (
        b'{"results":[{"title":"\\ud83c\\udf0d earth",'
        b'"url":"https://example.com/","snippet":"\\ud83c\\udf0d",'
        b'"summary":"s","content":"c","authority":1,"rerank_score":0.5}]}'
    )
    response_server, run = run_with(escaped_pair, ["search", "unicode probe"])
    assert run.code == 0, run.stdout + run.stderr
    assert decode(run.stdout)["results"][0]["title"] == "🌍 earth"

    valid_page = json.dumps({"title": "🌍", "content": "Body 🌍\nline"}, ensure_ascii=False).encode(
        "utf-8"
    )
    response_server, run = run_with(valid_page, ["fetch", "https://example.com/"])
    assert run.code == 0, run.stdout + run.stderr
    assert decode(run.stdout)["page"]["content"] == "Body 🌍\nline"

    # --- AC-20: --full cannot bypass the 8 MiB response cap ---
    oversized = _json_body_of_size(MAX_RESPONSE_BYTES + 1)
    for args in (
        ["search", "too large", "--full"],
        ["fetch", "https://example.com/", "--full"],
    ):
        response_server, run = run_with(oversized, args)
        assert run.code == 1, args
        assert run.stderr == ""
        document = decode(run.stdout)
        assert document["error"]["code"] == "response_too_large", args
        assert FAKE_KEY not in run.stdout + run.stderr
        # Exactly one attempt: no retry and no second hop.
        assert len(response_server.requests) == 1, args


def test_full_flag_does_not_bypass_schema_checks(stub: Any, tmp_path: Path) -> None:
    # --full changes what is rendered, never what is accepted: a 200 body with a
    # lone surrogate or a missing required field fails as invalid_response.
    project = tmp_path / "project"
    project.mkdir()
    invalid_bodies = [
        (
            "search-lone-surrogate",
            ["search", "full probe", "--full"],
            json.dumps({"results": [_search_item(title="bad\ud800")]}).encode("ascii"),
        ),
        (
            "search-missing-field",
            ["search", "full probe", "--full"],
            json.dumps({"results": [_without(_search_item(), "rerank_score")]}).encode("ascii"),
        ),
        (
            "fetch-lone-surrogate",
            ["fetch", "https://example.com/", "--full"],
            json.dumps({"title": "Title", "content": "bad\udfff"}).encode("ascii"),
        ),
        (
            "fetch-missing-content",
            ["fetch", "https://example.com/", "--full"],
            json.dumps({"title": "Title"}).encode("ascii"),
        ),
    ]
    for label, args, body in invalid_bodies:
        response_server = stub(lambda request, body=body: StubResponse(status=200, body=body))
        run_env = isolated_env(
            tmp_path / "cli-home",
            DEEPSEEK_API_KEY=FAKE_KEY,
            DEEPSEEK_BASE_URL=response_server.base_url,
        )
        run = run_cli(args, env=run_env, cwd=project)
        assert run.code == 1, label
        assert run.stderr == "", label
        assert decode(run.stdout)["error"]["code"] == "invalid_response", label
        assert "\ufffd" not in run.stdout, label
        assert len(response_server.requests) == 1, label


def test_search_body_key_order_and_optional_question() -> None:
    body = transport.build_search_body(
        SearchOptions(query="q", count=3, need_content=True, localization="1.2.3.4")
    )
    assert list(body) == ["query", "count", "need_content", "localization"]
    assert body == {
        "query": "q",
        "count": 3,
        "need_content": True,
        "localization": "1.2.3.4",
    }
    with_question = transport.build_search_body(
        SearchOptions(query="q", ori_question="original question")
    )
    assert list(with_question) == [
        "query",
        "count",
        "need_content",
        "localization",
        "ori_question",
    ]
    assert with_question["ori_question"] == "original question"

    without_question = transport.build_search_body(SearchOptions(query="q"))
    assert "ori_question" not in without_question
    assert without_question == {
        "query": "q",
        "count": 10,
        "need_content": False,
        "localization": "",
    }


def test_fetch_body_is_only_the_unchanged_url() -> None:
    url = "https://example.com/page?a=1&b=2#fragment"
    assert transport.build_fetch_body(url) == {"url": url}
    assert list(transport.build_fetch_body(url)) == ["url"]


def test_reject_raises_value_error_for_nonfinite_constants() -> None:
    for constant in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            transport._reject(constant)
