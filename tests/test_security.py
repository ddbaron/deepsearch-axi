"""Security acceptance tests for the single-request transport.

These are the origin-independent, in-process ``post_tool`` pieces: redirect
refusal, HTTPS verification, proxy/.netrc independence and origin re-assertion,
plus CLI-level redaction of keys reflected by home/session paths or origins.
"""

from __future__ import annotations

import http.client
import io
import json
import netrc
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support.cli import decode, run_cli
from support.env import FAKE_KEY, isolated_env
from support.stub_server import StubResponse, StubServer, json_response

from deepsearch_axi import transport
from deepsearch_axi._version import VERSION
from deepsearch_axi.cli import main
from deepsearch_axi.failures import status_failure
from deepsearch_axi.manifest import Manifest, manifest_bytes
from deepsearch_axi.models import (
    EXIT_FAILURE,
    REDACTED,
    TOOL_PATHS,
    Credential,
    DeepsearchError,
    HttpReply,
    SearchOptions,
    exit_code_for,
)


def _credential() -> Credential:
    return Credential(value=FAKE_KEY, source="env")


def _assert_no_secret_text(texts: tuple[str, ...]) -> None:
    for text in texts:
        assert FAKE_KEY not in text


class ResponseReadSpy:
    """Record every call to the response body readers."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._read = http.client.HTTPResponse.read
        self._readinto = http.client.HTTPResponse.readinto

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read = self._read
        readinto = self._readinto

        def spy_read(response: http.client.HTTPResponse, amt: int | None = None) -> bytes:
            self.calls.append("read")
            return read(response, amt)

        def spy_readinto(response: http.client.HTTPResponse, buffer: Any) -> int:
            self.calls.append("readinto")
            return readinto(response, buffer)

        monkeypatch.setattr(http.client.HTTPResponse, "read", spy_read)
        monkeypatch.setattr(http.client.HTTPResponse, "readinto", spy_readinto)


def test_redirects(stub: Callable[..., StubServer], monkeypatch: pytest.MonkeyPatch) -> None:
    credential = _credential()
    receiver = stub(lambda request: json_response({"received": FAKE_KEY}))
    assert receiver.requests == []

    for status in (301, 302, 303, 307, 308):
        marker = f"reflected-{status}"
        reflected = f'{{"error":"{marker}-{FAKE_KEY}"}}'.encode()
        location_box: dict[str, str] = {}

        def responder(
            request: Any,
            status: int = status,
            reflected: bytes = reflected,
            location_box: dict[str, str] = location_box,
        ) -> StubResponse:
            return StubResponse(
                status=status,
                body=reflected,
                headers={
                    "Location": location_box["location"],
                    "Content-Type": "application/json",
                },
            )

        redirector = stub(responder)
        # Exercise both same-origin and cross-origin targets.
        if status in (301, 303, 308):
            location = f"{redirector.base_url}/second-hop"
        else:
            location = f"{receiver.base_url}/second-hop"
        location_box["location"] = location
        spy = ResponseReadSpy()
        spy.install(monkeypatch)

        reply = transport.post_tool(
            redirector.base_url, "search", {"query": "redirect probe"}, credential
        )

        assert reply == HttpReply(status=status, payload=None)
        assert len(redirector.requests) == 1
        assert redirector.requests[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"
        assert receiver.requests == []
        assert spy.calls == []

        failure = status_failure(reply.status, "search")
        assert failure.code == "redirect_refused"
        assert failure.http_status == status
        assert exit_code_for(failure) == EXIT_FAILURE
        assert 1 <= len(failure.help) <= 2
        for text in (failure.message, *failure.help):
            assert "Location" not in text
            assert location not in text
            assert receiver.base_url not in text
            assert marker not in text
            _assert_no_secret_text((text,))
        monkeypatch.undo()


def test_origin_policy(
    stub: Callable[..., StubServer],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    credential = _credential()

    server = stub(lambda request: json_response({"ok": True}))
    options = SearchOptions(
        query="héllo 🌍", count=2, need_content=True, ori_question="why", localization=""
    )
    body = transport.build_search_body(options)
    reply = transport.post_tool(server.base_url + "/", "search", body, credential)
    assert reply == HttpReply(status=200, payload={"ok": True})
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.method == "POST"
    assert request.path == TOOL_PATHS["search"]
    assert request.json == body
    assert (
        request.body
        == (
            '{"query":"héllo 🌍","count":2,"need_content":true,"localization":"",'
            '"ori_question":"why"}'
        ).encode()
    )
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"] == f"deepsearch-axi/{VERSION}"

    fetch_url = "https://example.com/page?a=1&b=2#fragment"
    reply = transport.post_tool(
        server.base_url, "fetch", transport.build_fetch_body(fetch_url), credential
    )
    assert reply.status == 200
    assert server.requests[1].path == TOOL_PATHS["fetch"]
    assert server.requests[1].json == {"url": fetch_url}

    ipv6_server = stub(lambda request: json_response({"ok": True}), ipv6=True)
    reply = transport.post_tool(ipv6_server.base_url, "search", {"query": "q"}, credential)
    assert reply.status == 200
    assert len(ipv6_server.requests) == 1

    guard_server = stub(lambda request: json_response({"unexpected": True}))
    port = guard_server.server_address[1]
    invalid_origins = [
        f"http://localhost:{port}",
        f"http://192.0.2.1:{port}",
        f"http://user:pass@127.0.0.1:{port}",
        f"{guard_server.base_url}/v1",
        f"{guard_server.base_url}?debug=1",
        f"{guard_server.base_url}#fragment",
        f"http://127.0.0.1:{port}:extra",
        "ftp://127.0.0.1/tool",
        "not-a-url",
        "",
    ]
    for origin in invalid_origins:
        with pytest.raises(DeepsearchError) as rejected:
            transport.post_tool(origin, "search", {"query": "q"}, credential)
        assert rejected.value.failure.code == "invalid_config"
        assert rejected.value.failure.http_status is None
        assert exit_code_for(rejected.value.failure) == EXIT_FAILURE
        visible = (
            rejected.value.failure.message,
            *rejected.value.failure.help,
            repr(rejected.value),
        )
        _assert_no_secret_text(visible)
    assert guard_server.requests == []

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
    context = captured["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
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

    direct_server = stub(lambda request: json_response({"ok": True}))
    reply = transport.post_tool(direct_server.base_url, "search", {"query": "q"}, credential)
    assert reply.status == 200
    assert len(direct_server.requests) == 1
    headers = direct_server.requests[0].headers
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert "Proxy-Authorization" not in headers
    assert "netrcuser" not in headers["Authorization"]


# AC-01/AC-02 (security): home and session views must redact the effective key
# even when it is reflected by a path or by the validated API origin.


def test_home_redacts_env_key_reflected_in_origin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    project = tmp_path / "project"
    project.mkdir()
    env = isolated_env(
        home,
        DEEPSEEK_API_KEY=FAKE_KEY,
        DEEPSEEK_BASE_URL=f"https://{FAKE_KEY}.example.com",
    )
    run = run_cli([], env=env, cwd=project)
    assert run.code == 0, run.stderr
    document = decode(run.stdout)
    assert document["auth"] == {"state": "configured", "source": "env", "verified": False}
    assert document["api"] == {
        "origin": f"https://{REDACTED}.example.com",
        "state": "ready",
    }
    assert FAKE_KEY not in run.stdout
    assert FAKE_KEY not in run.stderr


def test_home_redacts_dotenv_key_reflected_in_origin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    env = isolated_env(home, DEEPSEEK_BASE_URL=f"https://{FAKE_KEY}.example.com")
    run = run_cli([], env=env, cwd=project)
    assert run.code == 0, run.stderr
    document = decode(run.stdout)
    assert document["auth"]["source"] == "cwd_dotenv"
    assert document["api"]["origin"] == f"https://{REDACTED}.example.com"
    assert FAKE_KEY not in run.stdout
    assert FAKE_KEY not in run.stderr


def test_session_redacts_dotenv_key_reflected_in_cwd(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    project = tmp_path / FAKE_KEY
    project.mkdir()
    (project / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    payload = json.dumps({"session_id": "session-1", "cwd": str(project)}).encode("utf-8")
    env = isolated_env(home)
    run = run_cli(
        ["session", "start", "--agent", "codex", "--project", str(project)],
        env=env,
        cwd=project,
        stdin=payload,
    )
    assert run.code == 0, run.stderr
    document = decode(run.stdout)
    assert REDACTED in document["cwd"]
    assert FAKE_KEY not in run.stdout
    assert FAKE_KEY not in run.stderr


def _opt_in(project: Path) -> Path:
    owned = project / ".deepsearch-axi"
    owned.mkdir()
    manifest = Manifest(
        project=str(project.resolve()),
        executable="/usr/local/bin/deepsearch-axi",
        command=("deepsearch-axi",),
        hosts={},
    )
    (owned / "manifest.json").write_bytes(manifest_bytes(manifest))
    return owned


def _keyed_child(project: Path) -> Path:
    child = project / FAKE_KEY
    child.mkdir()
    return child


def _assert_scope_is_opaque(state_text: str, container: str) -> None:
    assert FAKE_KEY not in state_text
    data = json.loads(state_text)
    if container == "scopes":
        scopes = list(data["scopes"])
    else:
        scopes = [record["scope"] for record in data["sessions"]]
    assert len(scopes) == 1
    assert scopes[0].startswith("o:")
    assert len(scopes[0]) == 34


_SEARCH_PAYLOAD = {
    "results": [
        {
            "title": "Example",
            "url": "https://example.com/",
            "snippet": "snippet",
            "summary": "summary",
            "content": "content",
            "authority": 1,
            "rerank_score": 0.5,
        }
    ]
}


@pytest.mark.parametrize("event", ["start", "end"])
def test_session_scope_persists_no_credential_from_child_cwd(
    tmp_path: Path, home_dir: Path, capsysbinary: pytest.CaptureFixture[bytes], event: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    owned = _opt_in(project)
    child = _keyed_child(project)
    home = home_dir
    payload = io.BytesIO(
        json.dumps({"session_id": f"review-{event}", "cwd": str(child)}).encode("utf-8")
    )
    code = main(
        ["session", event, "--agent", "codex", "--project", str(project)],
        env={"DEEPSEEK_API_KEY": FAKE_KEY},
        cwd=child,
        home=home,
        stdin=payload,
    )
    assert code == 0
    captured = capsysbinary.readouterr()
    assert FAKE_KEY.encode() not in captured.out + captured.err
    _assert_scope_is_opaque((owned / "state.json").read_text(encoding="utf-8"), "sessions")


@pytest.mark.parametrize("tool", ["search", "fetch"])
@pytest.mark.parametrize("status", [200, 500])
def test_research_scope_persists_no_credential_from_child_cwd(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch, tool: str, status: int
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    owned = _opt_in(project)
    child = _keyed_child(project)
    home = home_dir
    payload = _SEARCH_PAYLOAD if tool == "search" else {"title": "Example", "content": "content"}
    reply = HttpReply(status, payload if status == 200 else None)
    monkeypatch.setattr("deepsearch_axi.transport.post_tool", lambda *args, **kwargs: reply)
    args = ["search", "scope probe"] if tool == "search" else ["fetch", "https://example.com/"]
    code = main(
        args,
        env={"DEEPSEEK_API_KEY": FAKE_KEY},
        cwd=child,
        home=home,
    )
    assert code == (0 if status == 200 else 1)
    _assert_scope_is_opaque((owned / "state.json").read_text(encoding="utf-8"), "scopes")
