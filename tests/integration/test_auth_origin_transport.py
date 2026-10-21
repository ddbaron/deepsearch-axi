"""AC-09 integration seam: CLI validation and transport share one origin policy.

The rejected cases below are exactly the origins the canonical
``validate_base_url`` policy refuses, including the five retained regressions
(port zero, underscore hosts, empty ``?``/``#`` delimiters, scoped IPv6). They
are driven through the real CLI subprocess and through the importable
``post_tool`` with the connection factory instrumented, proving no socket is
constructed and no credential can be transmitted. Supporting cases prove
numeric-loopback HTTP, optional root slashes, documented endpoint paths,
verified TLS and proxy/.netrc independence still work.
"""

from __future__ import annotations

import netrc
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support.cli import decode, run_cli
from support.env import FAKE_KEY, isolated_env
from support.stub_server import StubServer, json_response

from deepsearch_axi import models, transport
from deepsearch_axi.cli import main
from deepsearch_axi.models import TOOL_PATHS, Credential, DeepsearchError
from deepsearch_axi.origin import validate_base_url

_REJECTED_ORIGINS = [
    pytest.param("http://127.0.0.1:0", id="zero-port"),
    pytest.param("https://under_score.example.com", id="underscore-host"),
    pytest.param("https://api.deepseek.com?", id="empty-query-delimiter"),
    pytest.param("https://api.deepseek.com#", id="empty-fragment-delimiter"),
    pytest.param("http://[::1%25lo]:8123", id="scoped-ipv6-loopback"),
    pytest.param("https://[fe80::1%25eth0]", id="scoped-ipv6"),
    pytest.param("http://localhost:8123", id="localhost-http"),
    pytest.param("http://192.0.2.1", id="arbitrary-http"),
    pytest.param("https://user:pass@example.com", id="userinfo"),
    pytest.param("https://api.deepseek.com/v1", id="path-prefix"),
    pytest.param("https://api.deepseek.com?debug=1", id="query"),
    pytest.param("https://api.deepseek.com#fragment", id="fragment"),
    pytest.param("ftp://api.deepseek.com", id="unsupported-scheme"),
    pytest.param("not-a-url", id="malformed"),
]


def _credential() -> Credential:
    return Credential(value=FAKE_KEY, source="env")


@pytest.mark.parametrize("origin", _REJECTED_ORIGINS)
def test_cli_rejects_validator_origins_before_transport(
    origin: str, tmp_path: Path, home_dir: Path
) -> None:
    # The retained set must be validator-rejected, not a transport-only list.
    with pytest.raises(DeepsearchError) as rejected:
        validate_base_url(origin)
    assert rejected.value.failure.code == "invalid_config"

    project = tmp_path / "project"
    project.mkdir()
    env = isolated_env(home_dir, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=origin)
    run = run_cli(["search", "origin seam"], env=env, cwd=project)

    assert run.code == 1, run.stdout
    assert run.stderr == ""
    document = decode(run.stdout)
    assert document["error"]["code"] == "invalid_config"
    assert FAKE_KEY not in run.stdout + run.stderr
    assert origin not in run.stdout + run.stderr


def test_cli_rejects_validator_origin_that_reflects_the_key(tmp_path: Path, home_dir: Path) -> None:
    origin = f"https://{FAKE_KEY}.example.com?debug=1"
    project = tmp_path / "project"
    project.mkdir()
    env = isolated_env(home_dir, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=origin)
    run = run_cli(["search", "origin seam"], env=env, cwd=project)

    assert run.code == 1, run.stdout
    assert run.stderr == ""
    assert decode(run.stdout)["error"]["code"] == "invalid_config"
    assert FAKE_KEY not in run.stdout + run.stderr


@pytest.mark.parametrize("origin", _REJECTED_ORIGINS)
def test_post_tool_rejects_validator_origins_before_opening(
    origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(DeepsearchError):
        validate_base_url(origin)

    def must_not_open(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"_open_connection was called for rejected origin {origin!r}")

    monkeypatch.setattr(transport, "_open_connection", must_not_open)

    with pytest.raises(DeepsearchError) as rejected:
        transport.post_tool(origin, "search", {"query": "q"}, _credential())

    assert rejected.value.failure.code == "invalid_config"
    assert rejected.value.failure.http_status is None
    visible = (
        rejected.value.failure.message,
        *rejected.value.failure.help,
        repr(rejected.value),
    )
    assert origin not in " ".join(visible)
    assert FAKE_KEY not in " ".join(visible)


@pytest.mark.parametrize("origin", _REJECTED_ORIGINS)
def test_cli_opens_no_connection_for_validator_origins(
    origin: str,
    tmp_path: Path,
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    # In-process companion to the subprocess checks: the connection factory is
    # the only socket seam in transport, so a zero count proves the CLI's own
    # validation stops before any connection is constructed.
    opened: list[tuple[Any, ...]] = []

    def record_open(*args: Any, **kwargs: Any) -> None:
        opened.append(args)
        raise AssertionError(f"_open_connection was called for rejected origin {origin!r}")

    monkeypatch.setattr(transport, "_open_connection", record_open)
    project = tmp_path / "project"
    project.mkdir()

    code = main(
        ["search", "origin seam"],
        env={"DEEPSEEK_API_KEY": FAKE_KEY, "DEEPSEEK_BASE_URL": origin},
        cwd=project,
        home=home_dir,
    )

    captured = capsysbinary.readouterr()
    assert code == 1
    assert opened == []
    assert captured.err == b""
    visible = captured.out.decode("utf-8")
    assert decode(visible)["error"]["code"] == "invalid_config"
    assert FAKE_KEY not in visible
    assert origin not in visible


def test_post_tool_keeps_a_direct_empty_origin_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the CLI maps unset/blank configuration to the official origin; a
    # directly imported transport must never silently retarget the credential.
    def must_not_open(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("_open_connection was called for an empty origin")

    monkeypatch.setattr(transport, "_open_connection", must_not_open)

    with pytest.raises(DeepsearchError) as rejected:
        transport.post_tool("", "search", {"query": "q"}, _credential())
    assert rejected.value.failure.code == "invalid_config"


def test_supported_origins_keep_paths_and_transport_guards(
    stub: Callable[..., StubServer],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    credential = _credential()

    assert validate_base_url(None) == models.OFFICIAL_ORIGIN
    assert validate_base_url("") == models.OFFICIAL_ORIGIN
    assert validate_base_url("https://api.deepseek.com/") == models.OFFICIAL_ORIGIN
    assert validate_base_url("https://example.com:8443/") == "https://example.com:8443"

    server = stub(lambda request: json_response({"results": []}))
    for origin in (server.base_url, server.base_url + "/"):
        reply = transport.post_tool(origin, "search", {"query": "seam"}, credential)
        assert reply.status == 200
        assert (
            transport.post_tool(
                origin, "fetch", {"url": "https://example.com/page"}, credential
            ).status
            == 200
        )

    assert [request.path for request in server.requests] == [
        TOOL_PATHS["search"],
        TOOL_PATHS["fetch"],
        TOOL_PATHS["search"],
        TOOL_PATHS["fetch"],
    ]
    assert all(
        request.headers["Authorization"] == f"Bearer {FAKE_KEY}" for request in server.requests
    )

    ipv6_server = stub(lambda request: json_response({"results": []}), ipv6=True)
    reply = transport.post_tool(ipv6_server.base_url, "search", {"query": "q"}, credential)
    assert reply.status == 200
    assert len(ipv6_server.requests) == 1
    assert ipv6_server.requests[0].path == TOOL_PATHS["search"]

    captured: dict[str, ssl.SSLContext] = {}
    real_create_default_context = ssl.create_default_context

    def record_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        context = real_create_default_context(*args, **kwargs)
        captured["context"] = context
        return context

    monkeypatch.setattr(transport.ssl, "create_default_context", record_context)
    tls_target = stub(lambda request: json_response({}))
    with pytest.raises(DeepsearchError) as tls_failure:
        transport.post_tool(
            f"https://127.0.0.1:{tls_target.server_address[1]}",
            "search",
            {"query": "q"},
            credential,
        )
    assert tls_failure.value.failure.code == "transport_error"
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["context"].check_hostname is True
    assert tls_target.requests == []
    monkeypatch.undo()

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    netrc_file = tmp_path / ".netrc"
    netrc_file.write_text(
        "machine 127.0.0.1 login netrcuser password netrcpass\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    def refuse_netrc(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(".netrc must not be read")

    monkeypatch.setattr(netrc, "netrc", refuse_netrc)

    direct_server = stub(lambda request: json_response({"results": []}))
    reply = transport.post_tool(direct_server.base_url, "search", {"query": "q"}, credential)
    assert reply.status == 200
    assert len(direct_server.requests) == 1
    headers = direct_server.requests[0].headers
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert "Proxy-Authorization" not in headers
