"""Error-surface acceptance tests: status mapping, transport failures, 200 schema.

These tests drive the real transport against the loopback stub.  Response
bodies are observed through ``HTTPResponse`` read instrumentation, so "non-200
bodies are never consumed" is proven rather than inferred.
"""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any

import pytest
from support.env import FAKE_KEY
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


def test_response_limits_and_schema(stub: Any) -> None:
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
