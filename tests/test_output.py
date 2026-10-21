"""Output contract tests: documents, projection, truncation, redaction, renderer.

The suite exercises pure document builders and decodes the rendered TOON with the
pinned library in strict mode, so exact decoded schemas, scalar types and
truncation metadata are asserted rather than snapshots.
"""

from __future__ import annotations

import io
import re
from dataclasses import replace
from pathlib import Path

import pytest
import toon_format
from support.env import FAKE_KEY

from deepsearch_axi import documents, render, transport
from deepsearch_axi.argv import parse_args
from deepsearch_axi.failures import (
    internal_error,
    missing_key,
    response_too_large,
    status_failure,
    usage_failure,
)
from deepsearch_axi.guidance import (
    COMMANDS,
    DESCRIPTION,
    HINT_FETCH,
    HINT_FETCH_FULL,
    HINT_SEARCH,
    HINT_SET_KEY,
    HINT_SETUP,
)
from deepsearch_axi.models import (
    DEFAULT_FIELDS,
    DEFAULT_FIELDS_WITH_CONTENT,
    OFFICIAL_ORIGIN,
    REDACTED,
    Activity,
    CredentialStatus,
    DeepsearchError,
    Failure,
    HostOutcome,
    LocalContext,
    OutputOptions,
    PriorEvent,
    SearchOptions,
    SetupResult,
    exit_code_for,
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "title": "Example title",
        "url": "https://example.com/article",
        "snippet": "Example snippet",
        "summary": "Example summary",
        "content": "Example content",
        "date": None,
        "authority": -1,
        "rerank_score": 0.5,
        "site_name": None,
        "site_icon": None,
    }
    row.update(overrides)
    return row


def _decode(document: dict[str, object], secrets: tuple[str, ...] = ()) -> object:
    return toon_format.decode(render.render_document(document, secrets), strict=True)


def _unicode_text(length: int) -> str:
    """A combining-mark string whose ``len`` is exactly ``length`` code points."""
    unit = "e\u0301"
    return unit * (length // 2) + ("e" if length % 2 else "")


def _context(**overrides: object) -> LocalContext:
    context = LocalContext(
        bin="~/bin/deepsearch-axi",
        cwd="~/projects/demo",
        auth=CredentialStatus("configured", "env"),
        origin=OFFICIAL_ORIGIN,
        origin_state="ready",
        integrations=(
            ("claude-code", "installed"),
            ("codex", "stale"),
            ("opencode", "missing"),
        ),
        prior=PriorEvent("codex", "start", 2),
        activity=Activity(search_ok=3, search_fail=1, fetch_ok=2, fetch_fail=0),
    )
    return replace(context, **overrides)


# --------------------------------------------------------------------------- #
# AC-14: TOON boundary, hostile strings, scalar types, render discipline
# --------------------------------------------------------------------------- #


def test_toon_boundary() -> None:
    hostile = {
        "toon": "a[2]{x}: 1,2",
        "colon": "key: value",
        "dash": "- item",
        "quotes": "\"quoted\" and 'single'",
        "commas": "one,two,three",
        "newlines": "line1\nline2\r\nline3",
        "numeric": "42",
        "truthy": "true",
        "nullable": "null",
        "array": "[1,2]",
        "object": "{a:1}",
        "ansi": "\x1b[31mred\x1b[0m",
        "unicode": "\u00e9\u0301\U0001f600\u4e2d",
        "colon_key": "value with: colon",
    }
    row = _row(
        title=hostile["toon"],
        snippet=hostile["quotes"],
        summary=hostile["numeric"],
        content=hostile["newlines"],
        date="2026-01-02",
        site_name="null",
        site_icon=None,
        authority=-1,
        rerank_score=2.5,
    )
    documents_under_test = [
        documents.search_document(
            'query "with" commas, colons:and quotes\ttabs',
            [row, _row(url=hostile["commas"], title=hostile["ansi"])],
            OutputOptions(DEFAULT_FIELDS_WITH_CONTENT + ("date", "site_name", "authority")),
        ),
        documents.search_document("no matches", [], OutputOptions()),
        documents.fetch_document(
            "https://example.com/a?x=1&y=2#frag",
            {"title": "", "content": "# Heading\n\n- one\n- two"},
        ),
        documents.error_document(status_failure(429, "search")),
        documents.error_document(usage_failure(("search",), "unknown flag --nope for search")),
        documents.help_document(()),
        documents.help_document(("search",)),
        documents.help_document(("session", "start")),
        documents.home_document(_context()),
        documents.ambient_document(_context(), "codex"),
        documents.setup_document(
            SetupResult(
                root=Path("/tmp/demo"),
                outcomes=(HostOutcome("claude-code", "installed", (".claude/settings.json",)),),
                changed=True,
            ),
            "~/demo",
        ),
        documents.session_end_document("end", True),
    ]

    for document in documents_under_test:
        text = render.render_document(document, ())
        decoded = toon_format.decode(text, strict=True)
        assert decoded == document
        assert list(decoded) == list(document)
        assert text == toon_format.encode(document, indent_size=2, delimiter=",")
        assert not text.endswith("\n")
        assert "\x1b" not in text
        assert not text.lstrip().startswith("{")

    decoded_row = toon_format.decode(
        render.render_document(
            documents.search_document("q", [row], OutputOptions(("title", "snippet", "summary"))),
            (),
        ),
        strict=True,
    )
    emitted = decoded_row["results"][0]
    assert emitted["title"] == hostile["toon"]
    assert emitted["snippet"] == hostile["quotes"]
    assert emitted["summary"] == "42"
    assert isinstance(emitted["summary"], str)
    assert row["date"] == "2026-01-02"
    assert row["authority"] == -1
    assert row["site_icon"] is None


def test_render_handles_surrogates_and_invalid_scalars() -> None:
    sanitized = _decode({"text": "bad \ud800 value", "a\udfff": "x\ud83d"})
    assert sanitized == {"text": "bad \ufffd value", "a\ufffd": "x\ufffd"}

    fallback = _decode({"value": object()})
    assert fallback == documents.error_document(internal_error())
    assert fallback["error"]["code"] == "internal_error"


def test_render_is_sole_toon_importer_and_stdout_writer() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "deepsearch_axi"
    importers = sorted(
        path.name
        for path in source_root.rglob("*.py")
        if re.search(
            r"^\s*(?:import|from)\s+toon_format\b",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    assert importers == ["render.py"]
    writers = {
        path.name
        for path in source_root.rglob("*.py")
        if re.search(
            r"sys\.stdout(?:\.buffer)?\.(?:write|flush)\b",
            path.read_text(encoding="utf-8"),
        )
    }
    assert writers <= {"render.py", "_entry.py"}


def test_write_output_and_broken_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Buffer:
        def __init__(self, *, fail: bool = False) -> None:
            self.data = bytearray()
            self.flushed = False
            self.fail = fail

        def write(self, payload: bytes) -> int:
            if self.fail:
                raise BrokenPipeError
            self.data.extend(payload)
            return len(payload)

        def flush(self) -> None:
            if self.fail:
                raise BrokenPipeError
            self.flushed = True

    class _Stdout:
        def __init__(self, *, fail: bool = False) -> None:
            self.buffer = _Buffer(fail=fail)

        def fileno(self) -> int:
            raise io.UnsupportedOperation("fileno")

    stdout = _Stdout()
    monkeypatch.setattr(render.sys, "stdout", stdout)
    render.write_output("")
    assert stdout.buffer.data == b""
    assert stdout.buffer.flushed is False
    render.write_output("h\u00e9llo\n")
    assert bytes(stdout.buffer.data) == "h\u00e9llo\n".encode()
    assert stdout.buffer.flushed is True

    broken = _Stdout(fail=True)
    monkeypatch.setattr(render.sys, "stdout", broken)
    render.write_output("ignored")  # must not raise


# --------------------------------------------------------------------------- #
# AC-15: projection
# --------------------------------------------------------------------------- #


def test_search_projection() -> None:
    row = _row(date=None, site_name=None, authority=-1, rerank_score=1.5)

    default = _decode(documents.search_document("q", [row], OutputOptions()))
    assert list(default["results"][0]) == ["title", "url", "snippet"]
    assert default["results"][0] == {
        "title": "Example title",
        "url": "https://example.com/article",
        "snippet": "Example snippet",
    }

    need_content = _decode(
        documents.search_document("q", [row], OutputOptions(DEFAULT_FIELDS_WITH_CONTENT))
    )
    assert list(need_content["results"][0]) == ["title", "url", "snippet", "content"]
    assert need_content["results"][0]["content"] == "Example content"

    fields = ("date", "title", "site_name", "authority", "rerank_score")
    explicit = _decode(documents.search_document("q", [row], OutputOptions(fields)))
    projected = explicit["results"][0]
    assert list(projected) == list(fields)
    assert projected["date"] is None
    assert projected["site_name"] is None
    assert projected["authority"] == -1
    assert projected["rerank_score"] == 1.5
    assert "content" not in projected
    assert "summary" not in projected


def test_search_projection_content_without_need_content_keeps_request_false() -> None:
    request = SearchOptions(query="struct concurrency")
    options = OutputOptions(("title", "content"))
    document = documents.search_document("struct concurrency", [_row()], options, request=request)
    decoded = _decode(document)
    assert request.need_content is False
    assert "--need-content" not in documents.search_flags_template(request, options, full=False)
    assert decoded["help"] == [
        'deepsearch-axi search "<query>" --count 10 --need-content --fields title,content',
        'deepsearch-axi search "<query> site:<domain>" --count 10 --fields title,content',
    ]


# --------------------------------------------------------------------------- #
# AC-16: totals, order, duplicates, definitive empty state
# --------------------------------------------------------------------------- #


def test_response_totals_and_empty() -> None:
    rows = [
        _row(url="https://example.com/a", title="A"),
        _row(url="https://example.com/b", title="B"),
        _row(url="https://example.com/a", title="A again"),
    ]
    decoded = _decode(documents.search_document("q", rows, OutputOptions()))
    assert list(decoded) == ["query", "count", "total", "total_scope", "results", "help"]
    assert decoded["count"] == 3
    assert decoded["total"] == 3
    assert decoded["total_scope"] == "response"
    assert [row["url"] for row in decoded["results"]] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/a",
    ]
    assert "message" not in decoded
    assert len(decoded["help"]) == 2

    request = SearchOptions(
        query="very specific query",
        count=4,
        need_content=True,
        ori_question="original ask",
        localization="::1",
    )
    empty = _decode(
        documents.search_document(
            "very specific query",
            [],
            OutputOptions(DEFAULT_FIELDS_WITH_CONTENT),
            request=request,
        )
    )
    assert list(empty) == [
        "query",
        "count",
        "total",
        "total_scope",
        "message",
        "results",
        "help",
    ]
    assert empty["query"] == "very specific query"
    assert empty["count"] == 0
    assert empty["total"] == 0
    assert empty["total_scope"] == "response"
    assert empty["message"] == "No results returned for this query."
    assert empty["results"] == []
    assert empty["help"] == [
        'deepsearch-axi search "<refined-query>" --count 4 --need-content '
        '--ori-question "<original-question>" --localization "<ip>"'
    ]
    assert "very specific query" not in " ".join(empty["help"])
    assert "original ask" not in " ".join(empty["help"])
    assert "::1" not in " ".join(empty["help"])


# --------------------------------------------------------------------------- #
# AC-17: fetch Markdown fidelity and unusable-content rejection
# --------------------------------------------------------------------------- #


def test_fetch_markdown() -> None:
    markdown = (
        "# Heading\n\n"
        "First paragraph with `code` and **emphasis**.\n\n"
        "- item one\n"
        "- item two\n\n"
        "```python\nprint('hi')\n```\n"
    )
    url = "https://example.com/path/page?x=1&y=2#fragment"
    document = documents.fetch_document(url, {"title": "Doc", "content": markdown})
    decoded = _decode(document)
    assert decoded == {"page": {"url": url, "title": "Doc", "content": markdown}}
    assert list(decoded) == ["page"]
    assert "truncated" not in decoded
    assert "help" not in decoded

    empty_title = _decode(documents.fetch_document(url, {"title": "", "content": "x"}))
    assert empty_title["page"]["title"] == ""
    assert empty_title["page"]["url"] == url

    for blank in ("", "   ", "\n\t \r\n", "\u00a0\u2003"):
        with pytest.raises(DeepsearchError) as info:
            documents.fetch_document(url, {"title": "Doc", "content": blank})
        assert info.value.failure.code == "invalid_response"
        assert exit_code_for(info.value.failure) == 1


# --------------------------------------------------------------------------- #
# AC-18: exact code-point truncation boundaries and metadata
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "limit"),
    [("title", 200), ("snippet", 200), ("summary", 1000), ("content", 1000)],
)
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_truncation_boundaries(field: str, limit: int, delta: int) -> None:
    length = limit + delta
    text = _unicode_text(length)
    document = documents.search_document("q", [_row(**{field: text})], OutputOptions((field,)))
    decoded = _decode(document)
    emitted = decoded["results"][0][field]
    if delta <= 0:
        assert emitted == text
        assert len(emitted) == length
        assert "truncated" not in decoded
        assert all("--full" not in hint for hint in decoded["help"])
    else:
        assert emitted == text[:limit]
        assert len(emitted) == limit
        assert "\u2026" not in emitted
        assert decoded["truncated"] == [
            {"path": f"results[0].{field}", "shown": limit, "total": length}
        ]
        assert decoded["help"][1].endswith("--full")


@pytest.mark.parametrize("text", ["\u00e9" * 201, "\U0001f600" * 201, "e\u0301" * 101])
def test_truncation_counts_unicode_code_points(text: str) -> None:
    decoded = _decode(documents.search_document("q", [_row(title=text)], OutputOptions(("title",))))
    assert decoded["results"][0]["title"] == text[:200]
    assert decoded["truncated"] == [{"path": "results[0].title", "shown": 200, "total": len(text)}]


def test_truncation_leaves_urls_dates_and_numbers_whole() -> None:
    long_url = "https://example.com/" + "a" * 400
    long_date = "2026-" + "0" * 400
    row = _row(
        url=long_url,
        date=long_date,
        authority=4,
        rerank_score=2.75,
        site_name="s" * 400,
        title="t" * 201,
    )
    fields = ("title", "url", "date", "authority", "rerank_score", "site_name")
    decoded = _decode(documents.search_document("q", [row], OutputOptions(fields)))
    emitted = decoded["results"][0]
    assert emitted["url"] == long_url
    assert emitted["date"] == long_date
    assert emitted["authority"] == 4
    assert emitted["rerank_score"] == 2.75
    assert emitted["site_name"] == "s" * 400
    assert emitted["title"] == "t" * 200
    assert decoded["truncated"] == [{"path": "results[0].title", "shown": 200, "total": 201}]


@pytest.mark.parametrize(("field", "limit"), [("title", 200), ("content", 1000)])
def test_fetch_truncation_boundaries(field: str, limit: int) -> None:
    url = "https://example.com/" + "a" * 300
    at_limit = {"title": "Doc", "content": "Body"}
    at_limit[field] = "\u00e9" * limit
    decoded = _decode(documents.fetch_document(url, at_limit))
    assert decoded["page"][field] == "\u00e9" * limit
    assert decoded["page"]["url"] == url
    assert "truncated" not in decoded
    assert "help" not in decoded

    over_limit = {"title": "Doc", "content": "Body"}
    over_limit[field] = "\u00e9" * (limit + 1)
    decoded = _decode(documents.fetch_document(url, over_limit))
    assert decoded["page"][field] == "\u00e9" * limit
    assert decoded["page"]["url"] == url
    assert decoded["truncated"] == [{"path": f"page.{field}", "shown": limit, "total": limit + 1}]
    assert decoded["help"] == [HINT_FETCH_FULL]


# --------------------------------------------------------------------------- #
# AC-19: --full, hint budget, placeholders and option retention
# --------------------------------------------------------------------------- #


def test_full_and_hints() -> None:
    row = _row(
        title="t" * 201,
        snippet="s" * 201,
        summary="m" * 1001,
        content="c" * 1001,
        date="2026-01-02",
        authority=3,
    )
    fields = ("title", "url", "snippet", "summary", "content", "date", "authority")
    preview = _decode(documents.search_document("q", [row], OutputOptions(fields)))
    full = _decode(documents.search_document("q", [row], OutputOptions(fields, full=True)))

    assert list(preview["results"][0]) == list(full["results"][0]) == list(fields)
    assert preview["count"] == full["count"] == 1
    assert preview["total"] == full["total"] == 1
    assert [item["path"] for item in preview["truncated"]] == [
        "results[0].title",
        "results[0].snippet",
        "results[0].summary",
        "results[0].content",
    ]
    assert "truncated" not in full
    assert full["results"][0]["content"] == "c" * 1001
    assert all("--full" not in hint for hint in full["help"])
    assert any("--full" in hint for hint in preview["help"])

    # --full is output-only: the parsed request and HTTP body are identical.
    preview_command = parse_args(["search", "runtime internals", "--count", "7", "--need-content"])
    full_command = parse_args(
        ["search", "runtime internals", "--count", "7", "--need-content", "--full"]
    )
    assert preview_command.options == full_command.options
    assert transport.build_search_body(preview_command.options) == transport.build_search_body(
        full_command.options
    )
    assert "--full does not raise the 8 MiB limit" in response_too_large("search").help[0]

    request = SearchOptions(
        query="runtime internals",
        count=7,
        need_content=True,
        ori_question="Where is the runtime?",
        localization="203.0.113.9",
    )
    retained = _decode(
        documents.search_document(
            "runtime internals",
            [row],
            OutputOptions(DEFAULT_FIELDS_WITH_CONTENT),
            request=request,
        )
    )
    assert request.query == "runtime internals"
    assert request.need_content is True
    assert retained["help"] == [
        HINT_FETCH,
        'deepsearch-axi search "<query>" --count 7 --need-content '
        '--ori-question "<original-question>" --localization "<ip>" --full',
    ]
    joined = " ".join(retained["help"])
    for runtime_value in (
        "runtime internals",
        "Where is the runtime?",
        "203.0.113.9",
        "https://example.com/article",
    ):
        assert runtime_value not in joined

    explicit_fields = _decode(
        documents.search_document(
            "q", [_row(content="c" * 1001)], OutputOptions(("title", "content"))
        )
    )
    assert explicit_fields["help"] == [
        'deepsearch-axi search "<query>" --count 10 --need-content --fields title,content',
        'deepsearch-axi search "<query>" --count 10 --fields title,content --full',
    ]

    default = _decode(documents.search_document("q", [_row()], OutputOptions()))
    assert default["help"] == [
        HINT_FETCH,
        'deepsearch-axi search "<query>" --count 10 --fields title,url,snippet,summary,date',
    ]

    # `search q --need-content --fields title,url,snippet` selects the default
    # projection explicitly; expansion must replace it, never add a second
    # --fields option that the production parser rejects.
    need_content_explicit_default = _decode(
        documents.search_document(
            "q",
            [_row()],
            OutputOptions(DEFAULT_FIELDS),
            request=SearchOptions(query="q", need_content=True),
        )
    )
    assert need_content_explicit_default["help"] == [
        HINT_FETCH,
        'deepsearch-axi search "<query>" --count 10 --need-content '
        "--fields title,url,snippet,summary,date",
    ]

    need_content_explicit_truncated = _decode(
        documents.search_document(
            "q",
            [_row(snippet="s" * 201)],
            OutputOptions(DEFAULT_FIELDS),
            request=SearchOptions(query="q", need_content=True),
        )
    )
    assert need_content_explicit_truncated["help"] == [
        HINT_FETCH,
        'deepsearch-axi search "<query>" --count 10 --need-content '
        "--fields title,url,snippet --full",
    ]

    non_default = _decode(
        documents.search_document("q", [_row()], OutputOptions(("title", "summary", "date")))
    )
    assert non_default["help"] == [
        HINT_FETCH,
        'deepsearch-axi search "<query> site:<domain>" --count 10 --fields title,summary,date',
    ]

    content_optin = documents.search_document(
        "q",
        [_row(content="c" * 1001)],
        OutputOptions(("title", "content")),
        request=SearchOptions(query="q"),
    )
    assert _decode(content_optin)["help"] == [
        'deepsearch-axi search "<query>" --count 10 --need-content --fields title,content',
        'deepsearch-axi search "<query>" --count 10 --fields title,content --full',
    ]

    for document in (
        preview,
        full,
        retained,
        explicit_fields,
        default,
        non_default,
        need_content_explicit_default,
        need_content_explicit_truncated,
    ):
        assert 1 <= len(document["help"]) <= 2
        for hint in document["help"]:
            assert hint.count("--fields") <= 1, f"repeated --fields in hint: {hint!r}"

    complete_fetch = _decode(
        documents.fetch_document("https://example.com/page", {"title": "T", "content": "Body"})
    )
    assert "help" not in complete_fetch and "truncated" not in complete_fetch


# --------------------------------------------------------------------------- #
# Error/help/home/ambient/setup/session-end decoded schemas
# --------------------------------------------------------------------------- #


def test_error_document_schema() -> None:
    failure = status_failure(429, "search")
    decoded = _decode(documents.error_document(failure))
    assert decoded == {
        "error": {
            "code": "rate_limited",
            "message": "DeepSeek search returned HTTP 429.",
            "http_status": 429,
        },
        "help": list(failure.help),
    }
    missing = _decode(documents.error_document(missing_key()))
    assert list(missing) == ["error", "help"]
    assert list(missing["error"]) == ["code", "message"]
    assert missing["error"]["code"] == "missing_key"
    assert 1 <= len(missing["help"]) <= 2


def test_help_document_schema() -> None:
    root = _decode(documents.help_document(()))
    assert list(root) == ["command", "description", "usage", "commands", "flags", "examples"]
    assert root["command"] == "deepsearch-axi"
    assert root["description"] == COMMANDS[()].summary
    assert root["usage"] == "deepsearch-axi [command] [flags]"
    assert [entry["name"] for entry in root["commands"]] == [
        "search",
        "fetch",
        "setup",
        "session",
    ]
    assert root["commands"][0]["description"] == COMMANDS[("search",)].summary
    assert root["flags"] == [
        {
            "flag": "-v, -V, --version",
            "default": "n/a",
            "description": "print the installed version and exit (sole argument only)",
        },
        {"flag": "--help", "default": "false", "description": "show help and exit"},
    ]

    search = _decode(documents.help_document(("search",)))
    assert list(search) == [
        "command",
        "description",
        "usage",
        "arguments",
        "flags",
        "examples",
    ]
    assert search["command"] == "deepsearch-axi search"
    assert search["arguments"] == [
        {
            "name": "query",
            "description": "1-128 characters, passed unchanged including operators",
        }
    ]
    assert [flag["flag"] for flag in search["flags"]] == [
        "--count N",
        "--need-content",
        "--ori-question TEXT",
        "--localization IP",
        "--fields FIELD,...",
        "--full",
        "--help",
    ]
    assert [flag["default"] for flag in search["flags"]] == [
        "10",
        "false",
        "omitted",
        "",
        "title,url,snippet",
        "false",
        "false",
    ]

    session_group = _decode(documents.help_document(("session",)))
    assert [entry["name"] for entry in session_group["commands"]] == ["start", "end"]
    assert "arguments" not in session_group
    start = _decode(documents.help_document(("session", "start")))
    assert [flag["flag"] for flag in start["flags"]] == [
        "--agent NAME",
        "--project ABS_DIR",
        "--help",
    ]
    assert start["flags"][0]["default"] == "required"

    for path in COMMANDS:
        text = render.render_document(documents.help_document(path), ())
        assert len(text) <= 4000
        assert "~/bin" not in text
        assert "DEEPSEEK_API_KEY=" not in text


def test_home_document_schema() -> None:
    context = _context()
    decoded = _decode(documents.home_document(context))
    assert list(decoded) == [
        "bin",
        "description",
        "cwd",
        "auth",
        "api",
        "integrations",
        "prior",
        "activity",
        "help",
    ]
    assert decoded["bin"] == "~/bin/deepsearch-axi"
    assert decoded["description"] == DESCRIPTION
    assert decoded["cwd"] == "~/projects/demo"
    assert decoded["auth"] == {"state": "configured", "source": "env", "verified": False}
    assert decoded["api"] == {"origin": OFFICIAL_ORIGIN, "state": "ready"}
    assert decoded["integrations"] == {
        "claude-code": "installed",
        "codex": "stale",
        "opencode": "missing",
    }
    assert decoded["prior"] == {"agent": "codex", "event": "start", "sessions": 2}
    assert decoded["activity"] == {
        "search_ok": 3,
        "search_fail": 1,
        "fetch_ok": 2,
        "fetch_fail": 0,
    }
    assert decoded["help"] == [HINT_SEARCH, HINT_SETUP]
    assert len(render.render_document(documents.home_document(context), ())) <= 1800

    minimal = replace(context, prior=None, activity=None)
    assert "prior" not in _decode(documents.home_document(minimal))
    assert "activity" not in _decode(documents.home_document(minimal))

    missing = _decode(
        documents.home_document(
            _context(
                auth=CredentialStatus("missing", "none"),
                integrations=(
                    ("claude-code", "installed"),
                    ("codex", "installed"),
                    ("opencode", "installed"),
                ),
            )
        )
    )
    assert missing["help"] == [HINT_SET_KEY, HINT_FETCH]

    invalid_key = _decode(
        documents.home_document(_context(auth=CredentialStatus("invalid", "cwd_dotenv")))
    )
    assert invalid_key["help"] == [
        "fix DEEPSEEK_API_KEY at its source; run deepsearch-axi for status",
        HINT_SETUP,
    ]

    invalid_origin = _decode(
        documents.home_document(_context(origin="invalid", origin_state="invalid"))
    )
    assert invalid_origin["help"] == [
        "unset DEEPSEEK_BASE_URL or set an https origin",
        HINT_SETUP,
    ]
    assert invalid_origin["api"] == {"origin": "invalid", "state": "invalid"}


def test_ambient_document_schema() -> None:
    context = _context()
    decoded = _decode(documents.ambient_document(context, "codex"))
    assert list(decoded) == [
        "bin",
        "description",
        "cwd",
        "auth",
        "api",
        "integration",
        "prior",
        "activity",
        "help",
    ]
    assert decoded["auth"] == {"state": "configured"}
    assert decoded["api"] == {"state": "ready"}
    assert decoded["integration"] == "stale"
    assert decoded["help"] == [HINT_SEARCH, HINT_FETCH]
    assert len(render.render_document(documents.ambient_document(context, "codex"), ())) <= 1200

    unconfigured = _decode(
        documents.ambient_document(
            _context(auth=CredentialStatus("missing", "none")), "claude-code"
        )
    )
    assert unconfigured["help"] == [HINT_SET_KEY, HINT_SEARCH]
    assert "source" not in unconfigured["auth"]
    assert "origin" not in unconfigured["api"]


def test_setup_and_session_end_document_schema() -> None:
    result = SetupResult(
        root=Path("/tmp/demo"),
        outcomes=(
            HostOutcome("claude-code", "installed", (".claude/settings.json",)),
            HostOutcome("codex", "unchanged", (".codex/hooks.json", ".codex/config.toml")),
        ),
        changed=True,
    )
    decoded = _decode(documents.setup_document(result, "~/demo"))
    assert decoded == {
        "setup": {"project": "~/demo", "changed": True},
        "hosts": [
            {"agent": "claude-code", "status": "installed", "files": ".claude/settings.json"},
            {
                "agent": "codex",
                "status": "unchanged",
                "files": ".codex/hooks.json;.codex/config.toml",
            },
        ],
        "help": ["start a new agent session to load deepsearch-axi context", HINT_SEARCH],
    }
    assert _decode(documents.session_end_document("end", True)) == {
        "session": {"event": "end", "recorded": True}
    }
    assert _decode(documents.session_end_document("checkpoint", False)) == {
        "session": {"event": "checkpoint", "recorded": False}
    }


def test_collapse_home() -> None:
    assert documents.collapse_home("/home/user/bin/deepsearch-axi", "/home/user") == (
        "~/bin/deepsearch-axi"
    )
    assert documents.collapse_home("/home/user", "/home/user") == "~"
    assert documents.collapse_home("/home/user/", "/home/user/") == "~"
    assert documents.collapse_home("/home/users/other", "/home/user") == "/home/users/other"
    assert documents.collapse_home("/opt/bin/deepsearch-axi", "/home/user") == (
        "/opt/bin/deepsearch-axi"
    )
    assert documents.collapse_home("/home/userx", "/home/user") == "/home/userx"
    assert documents.collapse_home("/home/user/bin", "") == "/home/user/bin"


# --------------------------------------------------------------------------- #
# AC-13 (portfolio): redaction before truncation and again at render
# --------------------------------------------------------------------------- #


def test_redact_recursive_values_and_keys() -> None:
    secret = FAKE_KEY
    assert documents.redact(f"a {secret} b", (secret,)) == f"a {REDACTED} b"
    assert documents.redact({"k-" + secret: [secret, 1, None, True, 1.5]}, (secret,)) == {
        "k-" + REDACTED: [REDACTED, 1, None, True, 1.5]
    }
    assert documents.redact(secret, ()) == secret
    assert documents.redact(secret, ("",)) == secret
    assert documents.redact(42, (secret,)) == 42
    assert documents.redact([secret, {"nested": secret}], (secret, "nested")) == [
        REDACTED,
        {REDACTED: REDACTED},
    ]


def test_redaction_happens_before_truncation() -> None:
    secret = FAKE_KEY
    request = SearchOptions(query=f"find {secret}")
    row = _row(
        title=f"{secret} title",
        content="x" * 1000 + secret,
    )
    document = documents.search_document(
        f"find {secret}",
        [row],
        OutputOptions(("title", "content")),
        request=request,
        secrets=(secret,),
    )
    decoded = _decode(document)
    assert secret not in render.render_document(document, ())
    assert decoded["query"] == f"find {REDACTED}"
    assert decoded["results"][0]["title"] == f"{REDACTED} title"
    assert decoded["results"][0]["content"] == "x" * 1000
    assert decoded["truncated"] == [
        {
            "path": "results[0].content",
            "shown": 1000,
            "total": 1000 + len(REDACTED),
        }
    ]
    # The request model keeps the real query; only the display is redacted.
    assert request.query == f"find {secret}"


def test_fetch_document_redacts_url_title_and_content() -> None:
    secret = FAKE_KEY
    url = f"https://example.com/{secret}"
    document = documents.fetch_document(
        url, {"title": secret, "content": f"body {secret}"}, secrets=(secret,)
    )
    decoded = _decode(document)
    assert decoded["page"] == {
        "url": "https://example.com/[REDACTED]",
        "title": REDACTED,
        "content": f"body {REDACTED}",
    }
    assert secret not in render.render_document(document, ())


def test_render_redacts_reflected_secret_again() -> None:
    secret = FAKE_KEY
    reflected = {
        "error": {"code": "unauthorized", "message": f"reflected {secret}"},
        "help": [f"replace {secret}"],
        f"key-{secret}": [f"nested {secret}"],
    }
    text = render.render_document(reflected, (secret,))
    assert secret not in text
    decoded = toon_format.decode(text, strict=True)
    assert decoded["error"]["message"] == f"reflected {REDACTED}"
    assert decoded["help"] == [f"replace {REDACTED}"]
    assert decoded[f"key-{REDACTED}"] == [f"nested {REDACTED}"]

    failure = Failure("custom", f"manual {secret}", (f"hint {secret}",))
    assert secret not in render.render_document(documents.error_document(failure), (secret,))
