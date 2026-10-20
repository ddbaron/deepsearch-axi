"""Validator tests: exact boundaries, literal values, and safe failures."""

from __future__ import annotations

import pytest

from deepsearch_axi import models, validation

_QUERY_ACCEPT = [
    pytest.param("a", id="one-char"),
    pytest.param("a" * 128, id="128-chars"),
    pytest.param(" e\u0301 ", id="nfd-surrounded"),
    pytest.param("e\u0301", id="nfd"),
    pytest.param("\u00e9", id="nfc"),
    pytest.param("\U0001f40d", id="emoji"),
    pytest.param("-site:example.com tokio", id="hyphen-leading"),
    pytest.param('"structured concurrency" -site:reddit.com', id="operators"),
    pytest.param("site:docs.python.org after:2025-01-01 OR lang:en", id="server-syntax"),
    pytest.param("a  b", id="double-space"),
]

_QUERY_REJECT = [
    pytest.param("", id="empty"),
    pytest.param(" ", id="space"),
    pytest.param("\t\n", id="tabs-newlines"),
    pytest.param("\u00a0", id="non-breaking-space"),
    pytest.param(" " * 128, id="all-whitespace"),
    pytest.param("a" * 129, id="129-chars"),
    pytest.param("\ud800", id="lone-high-surrogate"),
    pytest.param("a\udfff", id="lone-low-surrogate"),
]


@pytest.mark.parametrize("query", _QUERY_ACCEPT)
def test_validate_query_accepts_exact_values_unchanged(query: str) -> None:
    assert validation.validate_query(query) is None


@pytest.mark.parametrize("query", _QUERY_REJECT)
def test_validate_query_rejects_invalid_values(query: str) -> None:
    failure = _usage_failure(validation.validate_query, query)
    assert failure.message == "invalid value for <query>"
    assert "search" in failure.help[0]


_COUNT_ACCEPT = [
    pytest.param("1", 1, id="min"),
    pytest.param("10", 10, id="default"),
    pytest.param("50", 50, id="max"),
    pytest.param("007", 7, id="leading-zeros"),
    pytest.param("0" * 5000 + "5", 5, id="very-long-leading-zeros"),
]

_COUNT_REJECT = [
    pytest.param("0", id="zero"),
    pytest.param("51", id="above-max"),
    pytest.param("-1", id="negative"),
    pytest.param("+1", id="plus-sign"),
    pytest.param(" 1", id="leading-space"),
    pytest.param("1 ", id="trailing-space"),
    pytest.param("1.0", id="float"),
    pytest.param("1e2", id="exponent"),
    pytest.param("abc", id="noninteger"),
    pytest.param("", id="empty"),
    pytest.param("\u0661", id="non-ascii-digit"),
    pytest.param("50x", id="trailing-junk"),
    pytest.param("9" * 5000, id="huge-digit-string"),
]


@pytest.mark.parametrize(("token", "expected"), _COUNT_ACCEPT)
def test_parse_count_accepts_boundaries(token: str, expected: int) -> None:
    assert validation.parse_count(token) == expected


@pytest.mark.parametrize("token", _COUNT_REJECT)
def test_parse_count_rejects_invalid_tokens(token: str) -> None:
    failure = _usage_failure(validation.parse_count, token)
    assert failure.message == "invalid value for --count"


_ORI_ACCEPT = [
    pytest.param("x", id="one-char"),
    pytest.param("x" * 128, id="128-chars"),
    pytest.param(" ", id="whitespace-allowed"),
    pytest.param("\U0001f40d", id="emoji"),
    pytest.param("e\u0301", id="nfd"),
]

_ORI_REJECT = [
    pytest.param("", id="empty"),
    pytest.param("x" * 129, id="129-chars"),
    pytest.param("\ud800", id="surrogate"),
]


@pytest.mark.parametrize("text", _ORI_ACCEPT)
def test_validate_ori_question_accepts(text: str) -> None:
    assert validation.validate_ori_question(text) is None


@pytest.mark.parametrize("text", _ORI_REJECT)
def test_validate_ori_question_rejects(text: str) -> None:
    failure = _usage_failure(validation.validate_ori_question, text)
    assert failure.message == "invalid value for --ori-question"


_LOCALIZATION_ACCEPT = [
    pytest.param("", id="empty"),
    pytest.param("127.0.0.1", id="ipv4"),
    pytest.param("0.0.0.0", id="ipv4-zero"),
    pytest.param("255.255.255.255", id="ipv4-max"),
    pytest.param("::1", id="ipv6-loopback"),
    pytest.param("2001:db8::1", id="ipv6"),
]

_LOCALIZATION_REJECT = [
    pytest.param("localhost", id="hostname"),
    pytest.param("999.0.0.1", id="bad-octet"),
    pytest.param("127.0.0.1 ", id="trailing-space"),
    pytest.param(" 127.0.0.1", id="leading-space"),
    pytest.param("1.2.3", id="short-ipv4"),
    pytest.param("1.2.3.4.5", id="long-ipv4"),
    pytest.param("127.0.0.1/24", id="cidr"),
    pytest.param(":::", id="bad-ipv6"),
    pytest.param("not-an-ip", id="text"),
]


@pytest.mark.parametrize("value", _LOCALIZATION_ACCEPT)
def test_validate_localization_accepts(value: str) -> None:
    assert validation.validate_localization(value) is None


@pytest.mark.parametrize("value", _LOCALIZATION_REJECT)
def test_validate_localization_rejects(value: str) -> None:
    failure = _usage_failure(validation.validate_localization, value)
    assert failure.message == "invalid value for --localization"


_FIELDS_ACCEPT = [
    pytest.param("title", ("title",), id="single"),
    pytest.param("title,url", ("title", "url"), id="pair"),
    pytest.param(" title , url ", ("title", "url"), id="trimmed"),
    pytest.param(",".join(models.SEARCH_FIELDS), models.SEARCH_FIELDS, id="all-in-order"),
    pytest.param("content", ("content",), id="content-only"),
    pytest.param(
        "rerank_score,site_name,site_icon",
        ("rerank_score", "site_name", "site_icon"),
        id="scored-fields",
    ),
]

_FIELDS_REJECT = [
    pytest.param("", id="empty"),
    pytest.param(",", id="only-comma"),
    pytest.param("title,", id="trailing-comma"),
    pytest.param(",title", id="leading-comma"),
    pytest.param("title,,url", id="double-comma"),
    pytest.param("title,title", id="duplicate"),
    pytest.param(" title , title ", id="duplicate-trimmed"),
    pytest.param("Title", id="case-sensitive"),
    pytest.param("unknown", id="unknown"),
    pytest.param("titles", id="prefix-of-field"),
]


@pytest.mark.parametrize(("token", "expected"), _FIELDS_ACCEPT)
def test_parse_fields_accepts_allowlist(token: str, expected: tuple[str, ...]) -> None:
    assert validation.parse_fields(token) == expected


@pytest.mark.parametrize("token", _FIELDS_REJECT)
def test_parse_fields_rejects_invalid_projection(token: str) -> None:
    failure = _usage_failure(validation.parse_fields, token)
    assert failure.message == "invalid value for --fields"


def _padded_url(total: int) -> str:
    base = "https://example.com/"
    return base + "a" * (total - len(base))


_FETCH_ACCEPT = [
    pytest.param("https://example.com", id="https"),
    pytest.param("http://example.com", id="http"),
    pytest.param("https://example.com/a?b=1#c", id="query-fragment"),
    pytest.param("HTTPS://EXAMPLE.COM/Path", id="uppercase-scheme"),
    pytest.param("https://example.com:65535", id="max-port"),
    pytest.param("http://[::1]/x", id="ipv6"),
    pytest.param(_padded_url(8192), id="8192-chars"),
    pytest.param("https://example.com/a b", id="embedded-space"),
]

_FETCH_REJECT = [
    pytest.param("", id="empty"),
    pytest.param(_padded_url(8193), id="8193-chars"),
    pytest.param("https://example.com\x00", id="nul"),
    pytest.param("https://example.com\x1f", id="c0-control"),
    pytest.param("https://example.com\x7f", id="del"),
    pytest.param("https://example.com\u0080", id="c1-control"),
    pytest.param("https://example.com\u009f", id="c1-control-max"),
    pytest.param("https://example.com\ud800", id="surrogate"),
    pytest.param("example.com", id="no-scheme"),
    pytest.param("//example.com", id="scheme-relative"),
    pytest.param("ftp://example.com", id="ftp"),
    pytest.param("https://", id="no-host"),
    pytest.param("https:///path", id="empty-host"),
    pytest.param("https://user:pass@example.com", id="userinfo"),
    pytest.param("https://user@example.com", id="user"),
    pytest.param("https://example.com:abc", id="nonnumeric-port"),
    pytest.param("https://example.com:0", id="zero-port"),
    pytest.param("https://example.com:65536", id="large-port"),
    pytest.param("https://[::1", id="malformed-ipv6"),
    pytest.param("https://example.com:" + "9" * 5000, id="huge-port-digits"),
]


@pytest.mark.parametrize("url", _FETCH_ACCEPT)
def test_validate_fetch_url_accepts_unchanged(url: str) -> None:
    assert validation.validate_fetch_url(url) is None


@pytest.mark.parametrize("url", _FETCH_REJECT)
def test_validate_fetch_url_rejects_invalid_values(url: str) -> None:
    failure = _usage_failure(validation.validate_fetch_url, url)
    assert failure.message == "invalid value for <url>"
    assert "fetch" in failure.help[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", False),
        ("", False),
        ("\ud800", True),
        ("a\udfff", True),
        ("\U0001f40d", False),
    ],
)
def test_has_surrogates(text: str, expected: bool) -> None:
    assert validation.has_surrogates(text) is expected


@pytest.mark.parametrize(
    ("validator", "value", "marker"),
    [
        (validation.validate_query, "secret-marker" + "a" * 128, "secret-marker"),
        (validation.parse_count, "secret-marker", "secret-marker"),
        (validation.validate_ori_question, "secret-marker" + "a" * 128, "secret-marker"),
        (validation.validate_localization, "secret-marker", "secret-marker"),
        (validation.parse_fields, "secret-marker", "secret-marker"),
        (validation.validate_fetch_url, "ftp://secret-marker.example", "secret-marker"),
    ],
)
def test_failures_never_echo_the_rejected_value(validator, value: str, marker: str) -> None:
    with pytest.raises(models.DeepsearchError) as info:
        validator(value)
    failure = info.value.failure
    for text in (failure.message, *failure.help, repr(info.value), str(info.value)):
        assert marker not in text


def _usage_failure(validator, value: str) -> models.Failure:
    with pytest.raises(models.DeepsearchError) as info:
        validator(value)
    failure = info.value.failure
    assert failure.code == "usage"
    assert failure.http_status is None
    assert models.exit_code_for(failure) == models.EXIT_USAGE
    assert len(failure.help) == 2
    return failure
