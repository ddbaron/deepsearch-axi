"""CLI-level ``--full`` contract against the loopback stub.

``--full`` is an output-only switch: the request sent to the API must be byte
identical with and without it, the selected projection and count must match,
and the 8 MiB response cap must still apply. These tests run the real ``main``
entrypoint so the wiring from argv to transport to documents is exercised, not
only the pure builders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.cli import decode
from support.env import FAKE_KEY, isolated_env
from support.stub_server import StubResponse, json_response

from deepsearch_axi.cli import main
from deepsearch_axi.guidance import HINT_FETCH_FULL
from deepsearch_axi.models import MAX_RESPONSE_BYTES


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "title": "t" * 250,
        "url": "https://example.com/a",
        "snippet": "s" * 250,
        "summary": "m" * 1200,
        "content": "c" * 1200,
        "date": "2025-01-02",
        "authority": 1,
        "rerank_score": 2,
        "site_name": None,
        "site_icon": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return home, project


def _run(
    capsysbinary: pytest.CaptureFixture[bytes],
    argv: list[str],
    *,
    base_url: str,
    home: Path,
    project: Path,
) -> tuple[int, str, str]:
    env = isolated_env(home, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=base_url)
    code = main(argv, env=env, cwd=project, home=home)
    captured = capsysbinary.readouterr()
    return code, captured.out.decode("utf-8"), captured.err.decode("utf-8")


def test_full_search_sends_identical_body_and_keeps_projection(
    stub,
    workspace: tuple[Path, Path],
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    home, project = workspace
    rows = [_row(), _row(url="https://example.com/b")]
    server = stub(lambda request: json_response({"results": rows}))
    argv = [
        "search",
        "runtime internals",
        "--count",
        "3",
        "--need-content",
        "--fields",
        "title,summary,content",
    ]

    preview_code, preview_out, preview_err = _run(
        capsysbinary, argv, base_url=server.base_url, home=home, project=project
    )
    full_code, full_out, full_err = _run(
        capsysbinary, [*argv, "--full"], base_url=server.base_url, home=home, project=project
    )

    assert (preview_code, full_code) == (0, 0)
    assert preview_err == "" and full_err == ""
    assert len(server.requests) == 2
    first, second = server.requests
    assert first.method == second.method == "POST"
    assert first.path == second.path == "/v1/tools/search"
    assert first.body == second.body
    assert json.loads(first.body) == {
        "query": "runtime internals",
        "count": 3,
        "need_content": True,
        "localization": "",
    }

    preview = decode(preview_out)
    full = decode(full_out)
    assert preview["count"] == full["count"] == 2
    assert preview["total"] == full["total"] == 2
    assert (
        list(preview["results"][0])
        == list(full["results"][0])
        == [
            "title",
            "summary",
            "content",
        ]
    )
    assert len(full["results"]) == len(preview["results"]) == 2
    assert [len(row["summary"]) for row in full["results"]] == [1200, 1200]
    assert [len(row["content"]) for row in full["results"]] == [1200, 1200]
    assert [len(row["summary"]) for row in preview["results"]] == [1000, 1000]
    assert "truncated" not in full
    assert "truncated" in preview
    assert all("--full" not in hint for hint in full["help"])
    assert any("--full" in hint for hint in preview["help"])
    assert FAKE_KEY not in preview_out + full_out


def test_full_fetch_sends_identical_body_and_removes_only_truncation(
    stub,
    workspace: tuple[Path, Path],
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    home, project = workspace
    url = "https://example.com/page?x=1#frag"
    server = stub(
        lambda request: json_response({"title": "Doc", "content": "z" * 1500}),
    )

    preview_code, preview_out, _ = _run(
        capsysbinary, ["fetch", url], base_url=server.base_url, home=home, project=project
    )
    full_code, full_out, _ = _run(
        capsysbinary,
        ["fetch", url, "--full"],
        base_url=server.base_url,
        home=home,
        project=project,
    )

    assert (preview_code, full_code) == (0, 0)
    assert len(server.requests) == 2
    assert server.requests[0].path == server.requests[1].path == "/v1/tools/fetch"
    assert server.requests[0].body == server.requests[1].body
    assert json.loads(server.requests[0].body) == {"url": url}

    preview = decode(preview_out)
    full = decode(full_out)
    assert preview["page"]["url"] == full["page"]["url"] == url
    assert len(preview["page"]["content"]) == 1000
    assert full["page"]["content"] == "z" * 1500
    assert preview["help"] == [HINT_FETCH_FULL]
    assert "truncated" not in full and "help" not in full


def test_full_search_keeps_the_eight_mib_response_cap(
    stub,
    workspace: tuple[Path, Path],
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    home, project = workspace
    prefix = b'{"results":[],"pad":"'
    suffix = b'"}'
    over = prefix + b"a" * (MAX_RESPONSE_BYTES + 1 - len(prefix) - len(suffix)) + suffix
    server = stub(lambda request: StubResponse(status=200, body=over))

    code, out, err = _run(
        capsysbinary,
        ["search", "q", "--full"],
        base_url=server.base_url,
        home=home,
        project=project,
    )

    assert code == 1
    assert err == ""
    assert len(server.requests) == 1
    document = decode(out)
    assert document["error"]["code"] == "response_too_large"
    assert "--full does not raise the 8 MiB limit" in " ".join(document["help"])
    assert FAKE_KEY not in out


def test_full_search_accepts_exactly_the_eight_mib_response(
    stub,
    workspace: tuple[Path, Path],
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    home, project = workspace
    prefix = b'{"results":[],"pad":"'
    suffix = b'"}'
    at_limit = prefix + b"a" * (MAX_RESPONSE_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(at_limit) == MAX_RESPONSE_BYTES
    server = stub(lambda request: StubResponse(status=200, body=at_limit))

    code, out, err = _run(
        capsysbinary,
        ["search", "q", "--full"],
        base_url=server.base_url,
        home=home,
        project=project,
    )

    assert code == 0
    assert err == ""
    assert len(server.requests) == 1
    document = decode(out)
    assert document["count"] == 0
    assert document["results"] == []
    assert "truncated" not in document
