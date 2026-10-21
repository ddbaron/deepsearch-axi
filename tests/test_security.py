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

from deepsearch_axi import state, transport
from deepsearch_axi._version import VERSION
from deepsearch_axi.cli import main
from deepsearch_axi.failures import local_io, status_failure
from deepsearch_axi.manifest import Manifest, manifest_bytes
from deepsearch_axi.models import (
    EXIT_FAILURE,
    REDACTED,
    TOOL_PATHS,
    Credential,
    DeepsearchError,
    HttpReply,
    NativeEvent,
    SearchOptions,
    exit_code_for,
)
from deepsearch_axi.origin import validate_base_url


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

    # Count connection factories so the invalid-origin loop can prove that no
    # socket was even constructed, not merely that no request reached a stub.
    opened: list[tuple[str, str, int | None]] = []
    real_open_connection = transport._open_connection

    def record_open_connection(
        scheme: str, host: str, port: int | None, timeout_seconds: float
    ) -> http.client.HTTPConnection:
        opened.append((scheme, host, port))
        return real_open_connection(scheme, host, port, timeout_seconds)

    monkeypatch.setattr(transport, "_open_connection", record_open_connection)

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
        # Five retained regression cases: each is rejected by the canonical
        # origin validator but was previously accepted by the transport's
        # partial re-assertion.
        "http://127.0.0.1:0",
        "https://under_score.example.com",
        "https://api.deepseek.com?",
        "https://api.deepseek.com#",
        "https://[fe80::1%25eth0]",
        f"https://{FAKE_KEY}.example.com?debug=1",
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
        if origin:
            # Prove the retained set is exactly what the canonical validator
            # rejects, not an ad-hoc transport-only denylist.
            with pytest.raises(DeepsearchError):
                validate_base_url(origin)
    opened_before = len(opened)
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
    assert len(opened) == opened_before
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


def _written_files(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_redaction(
    tmp_path: Path,
    home_dir: Path,
    stub: Callable[..., StubServer],
    monkeypatch: pytest.MonkeyPatch,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    """AC-13: resolve-then-fail paths and reflected data never expose the key.

    The first block is the retained actual-subprocess regression: the project
    path itself contains the effective key and ``state.lock`` is a directory,
    so the command fails after secret resolution. Both env- and dotenv-selected
    credentials are exercised for session start and end.
    """
    for source in ("env", "dotenv"):
        for event in ("start", "end"):
            project = tmp_path / f"{source}-{event}" / FAKE_KEY
            project.mkdir(parents=True)
            owned = _opt_in(project)
            (owned / "state.lock").mkdir()
            if source == "env":
                env = isolated_env(home_dir, DEEPSEEK_API_KEY=FAKE_KEY)
            else:
                (project / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
                env = isolated_env(home_dir)
            payload = json.dumps(
                {"session_id": f"redaction-{source}-{event}", "cwd": str(project)}
            ).encode("utf-8")
            before = _written_files(project)
            run = run_cli(
                ["session", event, "--agent", "codex", "--project", str(project)],
                env=env,
                cwd=project,
                stdin=payload,
            )
            assert run.code == 1, f"{source}/{event}: {run.stdout}"
            assert run.stderr == ""
            document = decode(run.stdout)
            assert document["error"]["code"] == "local_io"
            assert FAKE_KEY not in run.stdout
            assert FAKE_KEY not in run.stderr
            assert not (owned / "state.json").exists()
            after = _written_files(project)
            # The command did not create or modify any persisted file, so no
            # state/configuration field can carry the resolved key either.
            assert set(after) <= set(before)
            for path, data in after.items():
                if before.get(path) != data:
                    assert FAKE_KEY.encode() not in data, path

    # The lock failure itself uses a safe tool-owned label, not the absolute
    # credential-bearing path, so direct library callers cannot leak it either.
    direct_project = tmp_path / "direct" / FAKE_KEY
    direct_project.mkdir(parents=True)
    direct_owned = _opt_in(direct_project)
    (direct_owned / "state.lock").mkdir()
    with pytest.raises(DeepsearchError) as lock_failure:
        state.record_session_event(
            direct_project,
            NativeEvent(session_hash="0" * 32, scope=".", kind="start"),
            "codex",
        )
    lock_visible = (
        lock_failure.value.failure.message,
        *lock_failure.value.failure.help,
        repr(lock_failure.value),
    )
    for text in lock_visible:
        assert FAKE_KEY not in text
        assert str(direct_project) not in text
    assert "state.lock" in lock_failure.value.failure.message

    reflected = f"title {FAKE_KEY} content"
    server = stub(
        lambda request: json_response(
            {
                "results": [
                    {
                        "title": reflected,
                        "url": "https://example.com/",
                        "snippet": reflected,
                        "summary": reflected,
                        "content": reflected,
                        "authority": 1,
                        "rerank_score": 0.5,
                    }
                ]
            }
        )
    )
    project = tmp_path / "success"
    project.mkdir()
    env = isolated_env(home_dir, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=server.base_url)
    query = f"site:example.com {FAKE_KEY} 🌍"
    run = run_cli(["search", query, "--need-content"], env=env, cwd=project)
    assert run.code == 0, run.stderr
    document = decode(run.stdout)
    assert document["query"] == f"site:example.com {REDACTED} 🌍"
    for row in document["results"]:
        assert row["title"] == f"title {REDACTED} content"
        assert row["content"] == f"title {REDACTED} content"
    assert FAKE_KEY not in run.stdout
    assert FAKE_KEY not in run.stderr
    assert len(server.requests) == 1
    # Redaction happens at the output boundary; the API receives the original
    # query text unchanged.
    assert server.requests[0].json["query"] == query

    page_server = stub(
        lambda request: json_response(
            {"title": f"Page {FAKE_KEY}", "content": f"Body {FAKE_KEY}\nmore"}
        )
    )
    env = isolated_env(home_dir, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=page_server.base_url)
    run = run_cli(["fetch", "https://example.com/page"], env=env, cwd=project)
    assert run.code == 0, run.stderr
    document = decode(run.stdout)
    assert document["page"]["title"] == f"Page {REDACTED}"
    assert document["page"]["content"] == f"Body {REDACTED}\nmore"
    assert FAKE_KEY not in run.stdout + run.stderr

    # Unsafe configuration never echoes the proposed origin.
    env = isolated_env(
        home_dir,
        DEEPSEEK_API_KEY=FAKE_KEY,
        DEEPSEEK_BASE_URL=f"https://{FAKE_KEY}.example.com/v1",
    )
    run = run_cli(["search", "probe"], env=env, cwd=project)
    assert run.code == 1
    assert decode(run.stdout)["error"]["code"] == "invalid_config"
    assert FAKE_KEY not in run.stdout + run.stderr

    # Usage-associated argv values are not echoed by usage failures.
    run = run_cli(["search", FAKE_KEY, "--not-a-flag"], env=isolated_env(home_dir), cwd=project)
    assert run.code == 2
    assert FAKE_KEY not in run.stdout + run.stderr

    # Non-200 headers and bodies are never surfaced.
    error_server = stub(
        lambda request: StubResponse(
            status=500,
            body=json.dumps({"error": FAKE_KEY, "marker": "reflected"}).encode("utf-8"),
            headers={"X-Echo": FAKE_KEY, "Location": f"https://{FAKE_KEY}.example.com/"},
        )
    )
    env = isolated_env(home_dir, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=error_server.base_url)
    run = run_cli(["search", "status"], env=env, cwd=project)
    assert run.code == 1
    assert decode(run.stdout)["error"]["code"] == "upstream_error"
    assert FAKE_KEY not in run.stdout + run.stderr
    assert len(error_server.requests) == 1

    # An injected exception message carrying an already-resolved key is
    # redacted at the output boundary rather than repaired away.
    injected = tmp_path / "injected"
    injected.mkdir()
    _opt_in(injected)
    native = json.dumps({"session_id": "injected", "cwd": str(injected)}).encode("utf-8")

    def fail_record(*args: Any, **kwargs: Any) -> bool:
        raise DeepsearchError(local_io(f"lock {FAKE_KEY}"))

    monkeypatch.setattr("deepsearch_axi.session.record_session_event", fail_record)
    code = main(
        ["session", "start", "--agent", "codex", "--project", str(injected)],
        env={"DEEPSEEK_API_KEY": FAKE_KEY},
        cwd=injected,
        home=home_dir,
        stdin=io.BytesIO(native),
    )
    captured = capsysbinary.readouterr()
    assert code == 1
    assert captured.err == b""
    injected_output = captured.out.decode("utf-8")
    assert FAKE_KEY not in injected_output
    assert REDACTED in injected_output
    monkeypatch.undo()

    def fail_post(*args: Any, **kwargs: Any) -> HttpReply:
        raise DeepsearchError(local_io(f"transport {FAKE_KEY}"))

    monkeypatch.setattr("deepsearch_axi.transport.post_tool", fail_post)
    code = main(
        ["search", "attempt"],
        env={"DEEPSEEK_API_KEY": FAKE_KEY},
        cwd=injected,
        home=home_dir,
    )
    captured = capsysbinary.readouterr()
    assert code == 1
    assert captured.err == b""
    assert FAKE_KEY not in captured.out.decode("utf-8")
