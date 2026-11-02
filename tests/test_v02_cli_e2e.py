"""End-to-end checks for the user intent restored in 0.2.

These tests invoke the installed command as a subprocess against a loopback
fake backend. They never import product code, so they observe only argv in,
process channels out, and the HTTP requests the product makes.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import toon_format

ROOT = Path(__file__).resolve().parents[1]
MISSING = object()


def _executable() -> str:
    configured = os.environ.get("DEEPSEARCH_AXI_EXE")
    if configured:
        return configured
    local = ROOT / ".venv" / "bin" / "deepsearch-axi"
    if local.exists():
        return str(local)
    found = shutil.which("deepsearch-axi")
    assert found, "install deepsearch-axi or set DEEPSEARCH_AXI_EXE"
    return found


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw)
        backend: FakeBackend = self.server.backend  # type: ignore[attr-defined]
        backend.requests.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if backend.replies:
            status, payload = backend.replies.pop(0)
        else:
            status, payload = 500, {"error": "fake backend has no scripted reply"}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        return


class FakeBackend:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.replies: list[tuple[int, object]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.backend = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def enqueue(self, payload: object, status: int = 200) -> None:
        self.replies.append((status, payload))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def backend() -> FakeBackend:
    fake = FakeBackend()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    return work, home


def _run(
    backend: FakeBackend,
    args: list[str],
    work: Path,
    home: Path,
    *,
    key: str | object = MISSING,
    tmp: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "DEEPSEARCH_AXI_BACKEND_URL": backend.url,
    }
    if tmp is not None:
        env["TMPDIR"] = str(tmp)
    if key is not MISSING:
        env["DEEPSEEK_API_KEY"] = str(key)
    return subprocess.run(
        [_executable(), *args],
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _decode(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, result.stdout + result.stderr
    decoded = toon_format.decode(result.stdout, strict=True)
    assert isinstance(decoded, dict)
    return decoded


def _result_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "title": "Python packaging guide",
        "url": "https://example.test/packaging",
        "snippet": "Package and distribute Python code.",
        "summary": "A longer summary that should stay unselected.",
        "content": "Page body " + ("x" * 1200),
        "date": "2026-01-02",
        "authority": 4,
        "rerank_score": 0.75,
        "site_name": "example.test",
        "site_icon": "https://example.test/icon.png",
    }
    row.update(overrides)
    return row


def _write_dotenv(path: Path, key: str, *, quoted: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = f'"{key}"' if quoted else key
    prefix = "export " if quoted else ""
    path.write_text(f"{prefix}DEEPSEEK_API_KEY={value}\n", encoding="utf-8")


def test_search_need_content_and_fields_projection(
    backend: FakeBackend, paths: tuple[Path, Path]
) -> None:
    work, home = paths
    row = _result_row()
    backend.enqueue({"results": [row]})

    result = _run(
        backend,
        [
            "search",
            "python packaging",
            "--limit",
            "2",
            "--need-content",
            "--fields",
            "title,url,snippet,date,content",
        ],
        work,
        home,
        key="sk-v02-test-search",
    )
    document = _decode(result)

    assert document["count"] == 1
    emitted = document["results"][0]
    assert list(emitted) == ["title", "url", "snippet", "date", "content"]
    assert emitted["date"] == "2026-01-02"
    assert emitted["content"] == row["content"]
    assert len(str(emitted["content"])) > 1000
    assert "summary" not in emitted
    assert backend.requests[-1]["path"] == "/v1/tools/search"
    assert backend.requests[-1]["body"] == {
        "query": "python packaging",
        "count": 2,
        "need_content": True,
    }

    backend.enqueue({"results": [row]})
    default_with_content = _decode(
        _run(
            backend,
            ["search", "python packaging", "--need-content"],
            work,
            home,
            key="sk-v02-test-search",
        )
    )
    assert list(default_with_content["results"][0]) == [
        "title",
        "url",
        "snippet",
        "content",
    ]

    backend.enqueue({"results": [row]})
    projection_only = _decode(
        _run(
            backend,
            ["search", "python packaging", "--fields", "title,url"],
            work,
            home,
            key="sk-v02-test-search",
        )
    )
    assert list(projection_only["results"][0]) == ["title", "url"]
    assert "need_content" not in backend.requests[-1]["body"]


@pytest.mark.parametrize(
    "fields",
    ["bogus", "title,title", "title,,url", "title,unknown,url"],
)
def test_fields_reject_unknown_blank_and_duplicate_names(
    backend: FakeBackend, paths: tuple[Path, Path], fields: str
) -> None:
    work, home = paths
    result = _run(
        backend,
        ["search", "query", "--fields", fields],
        work,
        home,
        key="sk-v02-test-fields",
    )
    assert result.returncode == 2
    assert "--fields" in (result.stdout + result.stderr).lower()
    assert "Traceback" not in (result.stdout + result.stderr)
    assert backend.requests == []


def test_fields_trim_spaces_around_names(backend: FakeBackend, paths: tuple[Path, Path]) -> None:
    work, home = paths
    backend.enqueue({"results": [_result_row()]})
    document = _decode(
        _run(
            backend,
            ["search", "query", "--fields", "title, url, date"],
            work,
            home,
            key="sk-v02-test-fields",
        )
    )
    assert list(document["results"][0]) == ["title", "url", "date"]


def test_credential_precedence_env_then_exact_cwd_then_user(
    backend: FakeBackend, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    env_key = "sk-v02-env-" + secrets.token_hex(8)
    cwd_key = "sk-v02-cwd-" + secrets.token_hex(8)
    user_key = "sk-v02-user-" + secrets.token_hex(8)
    _write_dotenv(work / ".env", cwd_key, quoted=True)
    _write_dotenv(home / ".config" / "deepsearch-axi" / ".env", user_key)

    backend.enqueue({"results": [_result_row()]})
    first = _run(backend, ["search", "query"], work, home, key=env_key)
    _decode(first)
    assert backend.requests[-1]["authorization"] == f"Bearer {env_key}"
    assert env_key not in first.stdout + first.stderr
    assert cwd_key not in first.stdout + first.stderr
    assert user_key not in first.stdout + first.stderr

    backend.enqueue({"results": [_result_row()]})
    second = _run(backend, ["search", "query"], work, home)
    _decode(second)
    assert backend.requests[-1]["authorization"] == f"Bearer {cwd_key}"
    assert cwd_key not in second.stdout + second.stderr
    assert user_key not in second.stdout + second.stderr

    (work / ".env").unlink()
    backend.enqueue({"results": [_result_row()]})
    third = _run(backend, ["search", "query"], work, home)
    _decode(third)
    assert backend.requests[-1]["authorization"] == f"Bearer {user_key}"
    assert user_key not in third.stdout + third.stderr


def test_credential_lookup_does_not_walk_parent_directories(
    backend: FakeBackend, tmp_path: Path
) -> None:
    parent = tmp_path / "parent"
    work = parent / "child"
    home = tmp_path / "home"
    work.mkdir(parents=True)
    home.mkdir()
    _write_dotenv(parent / ".env", "sk-v02-parent-only")

    result = _run(backend, ["search", "query"], work, home)

    assert result.returncode == 1
    assert "DEEPSEEK_API_KEY" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert backend.requests == []


def test_unknown_flag_never_reflects_credential_from_env_or_dotenv(
    backend: FakeBackend, paths: tuple[Path, Path]
) -> None:
    work, home = paths
    env_canary = "sk-v02-unknown-env-" + secrets.token_hex(12)
    dotenv_canary = "sk-v02-unknown-dotenv-" + secrets.token_hex(12)

    env_result = _run(
        backend,
        ["search", "query", f"--not-a-flag-{env_canary}"],
        work,
        home,
        key=env_canary,
    )
    assert env_result.returncode == 2
    assert env_canary not in env_result.stdout + env_result.stderr

    _write_dotenv(work / ".env", dotenv_canary)
    dotenv_result = _run(backend, [f"--{dotenv_canary}"], work, home)
    assert dotenv_result.returncode == 2
    assert dotenv_canary not in dotenv_result.stdout + dotenv_result.stderr

    command_result = _run(backend, [f"not-a-command-{dotenv_canary}"], work, home)
    assert command_result.returncode == 2
    assert dotenv_canary not in command_result.stdout + command_result.stderr
    assert backend.requests == []


def test_credentials_are_redacted_from_successful_urls_and_page_text(
    backend: FakeBackend, paths: tuple[Path, Path]
) -> None:
    work, home = paths
    canary = "sk-v02-output-" + secrets.token_hex(12)
    url = f"https://example.test/page?token={canary}"

    backend.enqueue({"title": "Example", "content": "y" * 1200})
    result = _run(backend, ["fetch", url], work, home, key=canary)
    document = _decode(result)
    assert canary not in result.stdout + result.stderr
    assert "[REDACTED]" in document["url"]
    assert document["url"] != url
    assert canary not in json.dumps(document)

    backend.enqueue(
        {
            "results": [
                _result_row(
                    title=f"Leaky title {canary}",
                    snippet=f"Leaky snippet {canary}",
                )
            ]
        }
    )
    search = _run(backend, ["search", "query"], work, home, key=canary)
    search_document = _decode(search)
    assert canary not in search.stdout + search.stderr
    assert canary not in json.dumps(search_document)
    assert "[REDACTED]" in json.dumps(search_document)


def test_missing_credential_names_all_sources(
    backend: FakeBackend, paths: tuple[Path, Path]
) -> None:
    work, home = paths
    result = _run(backend, ["doctor"], work, home)
    assert result.returncode == 1
    text = result.stdout + result.stderr
    assert "DEEPSEEK_API_KEY" in text
    assert ".env" in text
    assert "~/.config/deepsearch-axi/.env" in text
    assert "export" in text
    assert "Traceback" not in text
    assert backend.requests == []
