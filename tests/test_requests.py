"""AC-03..06: executable search/fetch request contract, preservation and validation.

Every case drives the real CLI in a subprocess against a loopback stub, so the
observed HTTP request is the product's own transport rather than a mocked
substitute. The stub records method, path, headers and the decoded JSON body.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from support.cli import decode, run_cli
from support.env import FAKE_KEY
from support.stub_server import StubServer, json_response

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

SEARCH_PAYLOAD = {
    "results": [
        {
            "title": "Example result",
            "url": "https://example.com/result",
            "snippet": "Example snippet",
            "summary": "Example summary",
            "content": "Example content",
            "date": None,
            "authority": 1,
            "rerank_score": 0.5,
            "site_name": None,
            "site_icon": None,
        }
    ]
}
FETCH_PAYLOAD = {"title": "Example page", "content": "# Heading\n\nBody text.\n"}


def _search_env(env: dict[str, str], server: StubServer) -> dict[str, str]:
    return dict(env, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=server.base_url)


def _assert_search_request(
    request: Any,
    *,
    expected_body: dict[str, object],
) -> None:
    assert request.method == "POST"
    assert request.path == SEARCH_PATH
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["Content-Type"] == "application/json"
    body = request.json
    assert body == expected_body
    assert list(body) == list(expected_body)
    assert type(body["count"]) is int
    assert type(body["need_content"]) is bool
    assert type(body["localization"]) is str
    assert type(body["query"]) is str


def test_search_contract(
    stub: Callable[..., StubServer], env: dict[str, str], project_dir: Path
) -> None:
    default_server = stub(lambda request: json_response(SEARCH_PAYLOAD))
    default_query = "site:python.org asyncio task group"
    default = run_cli(
        ["search", default_query],
        env=_search_env(env, default_server),
        cwd=project_dir,
    )
    assert default.code == 0, default.stderr
    assert default.stderr == ""
    assert len(default_server.requests) == 1
    _assert_search_request(
        default_server.requests[0],
        expected_body={
            "query": default_query,
            "count": 10,
            "need_content": False,
            "localization": "",
        },
    )
    decoded = decode(default.stdout)
    assert decoded["query"] == default_query
    assert decoded["count"] == 1

    explicit_server = stub(lambda request: json_response(SEARCH_PAYLOAD))
    explicit_query = "asyncio OR uvloop -site:reddit.com"
    explicit = run_cli(
        [
            "search",
            explicit_query,
            "--count",
            "7",
            "--need-content",
            "--ori-question",
            "how do task groups work",
            "--localization",
            "2001:db8::1",
            "--fields",
            "title,summary,date",
            "--full",
        ],
        env=_search_env(env, explicit_server),
        cwd=project_dir,
    )
    assert explicit.code == 0, explicit.stderr
    assert explicit.stderr == ""
    assert len(explicit_server.requests) == 1
    request = explicit_server.requests[0]
    _assert_search_request(
        request,
        expected_body={
            "query": explicit_query,
            "count": 7,
            "need_content": True,
            "localization": "2001:db8::1",
            "ori_question": "how do task groups work",
        },
    )
    body = request.json
    assert type(body["ori_question"]) is str
    assert body["ori_question"] == "how do task groups work"
    # Projection and preview flags are output-only and never transmitted.
    for output_only in ("fields", "full", "projection", "messages", "model", "tools", "stream"):
        assert output_only not in body


_QUERY_CASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("site:python.org asyncio TaskGroup",), "site:python.org asyncio TaskGroup"),
    (("asyncio -site:reddit.com -deprecated",), "asyncio -site:reddit.com -deprecated"),
    (("lang:en asyncio",), "lang:en asyncio"),
    (
        ("asyncio after:2025-01-01 before:2026-12-31",),
        "asyncio after:2025-01-01 before:2026-12-31",
    ),
    (("tokio OR asyncio OR uvloop",), "tokio OR asyncio OR uvloop"),
    (('"structured concurrency"',), '"structured concurrency"'),
    (('asyncio -"negative phrase"',), 'asyncio -"negative phrase"'),
    (
        ("path\\to\\thing C:\\temp\\file and \\\\double",),
        "path\\to\\thing C:\\temp\\file and \\\\double",
    ),
    (
        ('path C:\\temp\\"quoted" \\\\server',),
        'path C:\\temp\\"quoted" \\\\server',
    ),
    (("  asyncio   task group  ",), "  asyncio   task group  "),
    (("café naïve",), "café naïve"),
    (("cafe\u0301 nai\u0308ve",), "cafe\u0301 nai\u0308ve"),
    (("🙂 asyncio 🚀",), "🙂 asyncio 🚀"),
    (("--", "-site:example.com tokio"), "-site:example.com tokio"),
    (("a",), "a"),
    (("q" * 128,), "q" * 128),
    # 128 code points is the boundary whether they are astral emoji (512 UTF-8
    # bytes) or base-plus-combining pairs (two code points per visible character).
    (("🙂" * 128,), "🙂" * 128),
    (("e\u0301" * 64,), "e\u0301" * 64),
)


def test_query_preservation(
    stub: Callable[..., StubServer], env: dict[str, str], project_dir: Path
) -> None:
    server = stub(lambda request: json_response(SEARCH_PAYLOAD))
    environment = _search_env(env, server)
    for extra, expected in _QUERY_CASES:
        before = len(server.requests)
        run = run_cli(["search", *extra], env=environment, cwd=project_dir)
        assert run.code == 0, (extra, run.stderr)
        assert run.stderr == ""
        assert len(server.requests) == before + 1
        request = server.requests[-1]
        assert request.method == "POST"
        assert request.path == SEARCH_PATH
        body = request.json
        assert body["query"] == expected
        assert body["query"].encode("utf-8") == expected.encode("utf-8")


_INVALID_SEARCH_CASES: tuple[tuple[list[str], str], ...] = (
    (["search", ""], "invalid value for <query>"),
    (["search", "q" * 129], "invalid value for <query>"),
    (["search", " \t\n "], "invalid value for <query>"),
    (["search", "q", "--count", "0"], "invalid value for --count"),
    (["search", "q", "--count", "51"], "invalid value for --count"),
    (["search", "q", "--count", "ten"], "invalid value for --count"),
    (["search", "q", "--count", "1.5"], "invalid value for --count"),
    (["search", "q", "--count", "-1"], "missing value for --count"),
    (["search", "q", "--ori-question", ""], "invalid value for --ori-question"),
    (["search", "q", "--ori-question", "x" * 129], "invalid value for --ori-question"),
    # Length is counted in code points, not UTF-8 bytes or combined graphemes.
    (["search", "🙂" * 129], "invalid value for <query>"),
    (["search", "e\u0301" * 128], "invalid value for <query>"),
    (["search", "q", "--ori-question", "🙂" * 129], "invalid value for --ori-question"),
    (["search", "q", "--localization", "not-an-ip"], "invalid value for --localization"),
    (["search", "q", "--localization", "999.999.999.999"], "invalid value for --localization"),
    (["search", "q", "--localization", "localhost"], "invalid value for --localization"),
    (["search", "q", "--fields", "title,title"], "invalid value for --fields"),
    (["search", "q", "--fields", "title,bogus"], "invalid value for --fields"),
    (["search", "q", "--fields", ""], "invalid value for --fields"),
    (["search", "q", "--fields", "title,,url"], "invalid value for --fields"),
    (["search", "q", "--fields", ",title"], "invalid value for --fields"),
)

_VALID_SEARCH_CASES: tuple[tuple[list[str], dict[str, object]], ...] = (
    (["search", "q", "--count", "1"], {"count": 1}),
    (["search", "q", "--count", "50"], {"count": 50}),
    (["search", "q"], {"localization": ""}),
    (["search", "q", "--localization", "192.0.2.44"], {"localization": "192.0.2.44"}),
    (["search", "q", "--localization", "2001:db8::5"], {"localization": "2001:db8::5"}),
    # A zone-scoped literal is still an IPv6 address; the value is not rewritten.
    (["search", "q", "--localization", "fe80::1%eth0"], {"localization": "fe80::1%eth0"}),
    (["search", "q", "--ori-question", "x"], {"ori_question": "x"}),
    (["search", "q", "--ori-question", "x" * 128], {"ori_question": "x" * 128}),
    (["search", "q", "--ori-question", "   "], {"ori_question": "   "}),
    (["search", "q", "--ori-question", "🙂" * 128], {"ori_question": "🙂" * 128}),
)


def test_search_validation(
    stub: Callable[..., StubServer], env: dict[str, str], project_dir: Path
) -> None:
    server = stub(lambda request: json_response(SEARCH_PAYLOAD))
    # A usable credential exists only in ./.env: any invalid invocation must
    # fail before the file is read and before the stub is contacted.
    (project_dir / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    environment = dict(env, DEEPSEEK_BASE_URL=server.base_url)

    for args, message in _INVALID_SEARCH_CASES:
        before = len(server.requests)
        run = run_cli(
            args,
            env=environment,
            cwd=project_dir,
            read_guard=[project_dir / ".env"],
        )
        assert run.code == 2, (args, run.stdout, run.stderr)
        assert run.stderr == ""
        document = decode(run.stdout)
        assert document["error"]["code"] == "usage"
        assert message in document["error"]["message"]
        assert 1 <= len(document["help"]) <= 2
        assert document["help"][0].startswith("valid flags for search:")
        for flag in ("--count N", "--need-content", "--ori-question TEXT", "--fields FIELD,..."):
            assert flag in document["help"][0]
        assert document["help"][1].startswith("deepsearch-axi search")
        assert len(server.requests) == before
        assert run.reads == ()

    (project_dir / ".env").unlink()
    for args, overrides in _VALID_SEARCH_CASES:
        before = len(server.requests)
        run = run_cli(args, env=_search_env(env, server), cwd=project_dir)
        assert run.code == 0, (args, run.stdout, run.stderr)
        body = server.requests[-1].json
        expected = {
            "query": "q",
            "count": 10,
            "need_content": False,
            "localization": "",
        }
        expected.update(overrides)
        assert body == expected
        assert list(body) == list(expected)
        assert len(server.requests) == before + 1


_INVALID_FETCH_CASES: tuple[str, ...] = (
    "",
    "ftp://example.com/file",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "https://",
    "https:///path",
    "https://user:pass@example.com/article",
    "https://example.com:0/article",
    "https://example.com:65536/article",
    "https://example.com:notaport/article",
    "https://example.com/\x01control",
    "https://example.com/line\nbreak",
    "https://example.com/" + "a" * 8183,
    # Exactly one character over the 8192-character limit.
    "https://example.com/" + "a" * (8193 - len("https://example.com/")),
)


def test_fetch_contract(
    stub: Callable[..., StubServer], env: dict[str, str], project_dir: Path
) -> None:
    api_server = stub(lambda request: json_response(FETCH_PAYLOAD))
    target_server = stub(lambda request: json_response({"unexpected": True}))
    environment = _search_env(env, api_server)
    boundary = "https://example.com/" + "a" * (8192 - len("https://example.com/"))
    assert len(boundary) == 8192
    # 8192 code points but roughly 32 KiB of UTF-8: the limit counts characters.
    emoji_boundary = "https://example.com/" + "🙂" * (8192 - len("https://example.com/"))
    assert len(emoji_boundary) == 8192
    target_url = target_server.base_url + "/page?x=1&y=2#fragment"
    valid_urls = (
        "https://example.com/article",
        "http://example.com/page?x=1&y=2#fragment",
        "https://example.com/a%20b?x=1&y=%2F",
        "http://127.0.0.1:8080/path/%E2%9C%93",
        target_url,
        boundary,
        emoji_boundary,
    )
    for url in valid_urls:
        before = len(api_server.requests)
        run = run_cli(["fetch", url], env=environment, cwd=project_dir)
        assert run.code == 0, (url, run.stdout, run.stderr)
        assert run.stderr == ""
        assert len(api_server.requests) == before + 1
        request = api_server.requests[-1]
        assert request.method == "POST"
        assert request.path == FETCH_PATH
        assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
        assert request.headers["Content-Type"] == "application/json"
        assert request.json == {"url": url}
        assert list(request.json) == ["url"]
        # The CLI never contacts the target page itself.
        assert target_server.requests == []

    for url in _INVALID_FETCH_CASES:
        before = len(api_server.requests)
        run = run_cli(["fetch", url], env=environment, cwd=project_dir)
        assert run.code == 2, (url, run.stdout, run.stderr)
        assert run.stderr == ""
        document = decode(run.stdout)
        assert document["error"]["code"] == "usage"
        assert "invalid value for <url>" in document["error"]["message"]
        assert 1 <= len(document["help"]) <= 2
        assert document["help"][0].startswith("valid flags for fetch:")
        assert "--full" in document["help"][0]
        assert document["help"][1].startswith("deepsearch-axi fetch")
        assert len(api_server.requests) == before
        assert target_server.requests == []
        assert "user:pass" not in run.stdout
