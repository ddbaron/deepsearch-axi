"""Origin policy tests for ``DEEPSEEK_BASE_URL``."""

from __future__ import annotations

import pytest

from deepsearch_axi import models
from deepsearch_axi.origin import validate_base_url

_ACCEPT = [
    pytest.param(None, models.OFFICIAL_ORIGIN, id="unset"),
    pytest.param("", models.OFFICIAL_ORIGIN, id="empty"),
    pytest.param("https://api.deepseek.com", "https://api.deepseek.com", id="official"),
    pytest.param("https://api.deepseek.com/", "https://api.deepseek.com", id="official-slash"),
    pytest.param("https://example.com", "https://example.com", id="custom-https"),
    pytest.param("https://example.com:8443", "https://example.com:8443", id="custom-port"),
    pytest.param(
        "https://sub.example.com:443/", "https://sub.example.com:443", id="custom-port-slash"
    ),
    pytest.param("http://127.0.0.1", "http://127.0.0.1", id="ipv4-loopback"),
    pytest.param("http://127.0.0.1:8123", "http://127.0.0.1:8123", id="ipv4-loopback-port"),
    pytest.param("http://127.0.0.1:8123/", "http://127.0.0.1:8123", id="ipv4-loopback-slash"),
    pytest.param("http://[::1]:8123", "http://[::1]:8123", id="ipv6-loopback"),
    pytest.param("http://[::1]/", "http://[::1]", id="ipv6-loopback-slash"),
    pytest.param("https://127.0.0.1", "https://127.0.0.1", id="https-loopback"),
    pytest.param("https://[::1]:443", "https://[::1]:443", id="https-ipv6-loopback"),
    pytest.param("HTTPS://EXAMPLE.COM/", "https://EXAMPLE.COM", id="uppercase"),
]

_REJECT = [
    pytest.param("http://example.com", id="plain-http"),
    pytest.param("http://localhost:8123", id="localhost-http"),
    pytest.param("http://localhost", id="localhost-no-port"),
    pytest.param("http://[::2]:8123", id="non-loopback-ipv6"),
    pytest.param("http://192.168.0.1", id="private-http"),
    pytest.param("ftp://example.com", id="ftp"),
    pytest.param("file:///tmp/x", id="file"),
    pytest.param("api.deepseek.com", id="no-scheme"),
    pytest.param("//api.deepseek.com", id="scheme-relative"),
    pytest.param("https:/example.com", id="one-slash"),
    pytest.param("https://", id="no-host"),
    pytest.param("https:///path", id="empty-host"),
    pytest.param("https://u:p@example.com", id="userinfo"),
    pytest.param("https://u@example.com", id="user"),
    pytest.param("https://api.deepseek.com/v1", id="v1-prefix"),
    pytest.param("https://api.deepseek.com/v1/tools/search", id="api-path"),
    pytest.param("https://api.deepseek.com/path", id="nonroot-path"),
    pytest.param("https://api.deepseek.com//", id="double-slash"),
    pytest.param("https://api.deepseek.com?x=1", id="query"),
    pytest.param("https://api.deepseek.com?", id="empty-query"),
    pytest.param("https://api.deepseek.com#frag", id="fragment"),
    pytest.param("https://api.deepseek.com#", id="empty-fragment"),
    pytest.param("https://api.deepseek.com:abc", id="nonnumeric-port"),
    pytest.param("https://api.deepseek.com:0", id="zero-port"),
    pytest.param("https://api.deepseek.com:65536", id="large-port"),
    pytest.param("https://api.deepseek.com:" + "9" * 5000, id="huge-port-digits"),
    pytest.param("https://api.deepseek.com:-1", id="negative-port"),
    pytest.param(" https://api.deepseek.com", id="leading-space"),
    pytest.param("https://api.deepseek.com ", id="trailing-space"),
    pytest.param("https://api.deepseek.com/a b", id="embedded-space"),
    pytest.param("\thttps://api.deepseek.com", id="tab"),
    pytest.param("https://api.deepseek.com\n", id="newline"),
    pytest.param("https://api.deepseek.com\x00", id="nul"),
    pytest.param("https://api.deepseek.com\x1f", id="c0-control"),
    pytest.param("https://api.deepseek.com\x7f", id="del"),
    pytest.param("https://api.deepseek.com\u0080", id="c1-control"),
    pytest.param("https://api.deepseek.com\ud800", id="surrogate"),
    pytest.param("https://[::1", id="malformed-ipv6"),
    pytest.param(":", id="colon"),
    pytest.param(" ", id="whitespace"),
    pytest.param("?", id="question-mark"),
]


@pytest.mark.parametrize(("value", "expected"), _ACCEPT)
def test_origin_accepts_and_normalizes(value: str | None, expected: str) -> None:
    assert validate_base_url(value) == expected


@pytest.mark.parametrize("value", _REJECT)
def test_origin_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(models.DeepsearchError) as info:
        validate_base_url(value)
    failure = info.value.failure
    assert failure.code == "invalid_config"
    assert failure.http_status is None
    assert models.exit_code_for(failure) == models.EXIT_FAILURE
    assert failure.message == "DEEPSEEK_BASE_URL is not a valid trusted API origin."
    assert len(failure.help) == 2
    assert "https://api.deepseek.com" in failure.help[0]
    assert "127.0.0.1" in failure.help[1]


@pytest.mark.parametrize(
    "value",
    [
        "ftp://secret-marker.invalid",
        "https://u:p@secret-marker.invalid",
        "https://secret-marker.invalid/v1",
        "http://secret-marker.invalid",
        "https://secret-marker.invalid?token=secret-marker",
    ],
)
def test_origin_failures_do_not_echo_the_value(value: str) -> None:
    with pytest.raises(models.DeepsearchError) as info:
        validate_base_url(value)
    failure = info.value.failure
    for text in (failure.message, *failure.help, repr(info.value), str(info.value)):
        assert "secret-marker" not in text
