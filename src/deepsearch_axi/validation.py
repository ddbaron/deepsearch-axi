"""Input validators for search and fetch arguments.

Every validator raises :class:`~deepsearch_axi.models.DeepsearchError` carrying a
``usage`` failure with a fixed, value-free message. Valid values are never
modified: callers pass the original text through unchanged.
"""

from __future__ import annotations

import ipaddress
import re
from typing import NoReturn
from urllib.parse import urlsplit

from deepsearch_axi.failures import usage_failure
from deepsearch_axi.models import (
    FETCH_URL_MAX,
    ORI_QUESTION_MAX,
    QUERY_MAX,
    SEARCH_FIELDS,
    DeepsearchError,
)

_COUNT_RE = re.compile(r"[0-9]+")
_SEARCH_PATH = ("search",)
_FETCH_PATH = ("fetch",)
_URL_PATH_MESSAGE = "invalid value for <url>"


def _fail(path: tuple[str, ...], message: str) -> NoReturn:
    raise DeepsearchError(usage_failure(path, message))


def has_surrogates(text: str) -> bool:
    """Return True when ``text`` contains any lone UTF-16 surrogate code point."""
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def validate_query(query: str) -> None:
    """Accept 1..128 non-blank code points with no surrogates, unchanged."""
    if not 1 <= len(query) <= QUERY_MAX or query.isspace() or has_surrogates(query):
        _fail(_SEARCH_PATH, "invalid value for <query>")


def parse_count(token: str) -> int:
    """Parse an ASCII decimal result count in 1..50."""
    if _COUNT_RE.fullmatch(token) is None:
        _fail(_SEARCH_PATH, "invalid value for --count")
    value = int(token)
    if not 1 <= value <= 50:
        _fail(_SEARCH_PATH, "invalid value for --count")
    return value


def validate_ori_question(text: str) -> None:
    """Accept 1..128 code points with no surrogates; whitespace is allowed."""
    if not 1 <= len(text) <= ORI_QUESTION_MAX or has_surrogates(text):
        _fail(_SEARCH_PATH, "invalid value for --ori-question")


def validate_localization(value: str) -> None:
    """Accept an empty string or any literal IPv4/IPv6 address, unchanged."""
    if value == "":
        return
    try:
        ipaddress.ip_address(value)
    except ValueError:
        _fail(_SEARCH_PATH, "invalid value for --localization")


def parse_fields(token: str) -> tuple[str, ...]:
    """Split an ordered field projection, rejecting empty/unknown/duplicate items."""
    fields = tuple(part.strip() for part in token.split(","))
    if (
        not fields
        or any(not field or field not in SEARCH_FIELDS for field in fields)
        or len(set(fields)) != len(fields)
    ):
        _fail(_SEARCH_PATH, "invalid value for --fields")
    return fields


def _has_forbidden_url_code(url: str) -> bool:
    for char in url:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            return True
        if 0xD800 <= code <= 0xDFFF:
            return True
    return False


def validate_fetch_url(url: str) -> None:
    """Validate an input HTTP(S) URL without rewriting it.

    Rejects anything outside 1..8192 code points, embedded control characters
    and surrogates, non-HTTP(S) schemes, userinfo, a missing host, and a port
    that is unparseable or outside 1..65535.
    """
    if not 1 <= len(url) <= FETCH_URL_MAX or _has_forbidden_url_code(url):
        _fail(_FETCH_PATH, _URL_PATH_MESSAGE)
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        _fail(_FETCH_PATH, _URL_PATH_MESSAGE)
    if parts.scheme.lower() not in {"http", "https"}:
        _fail(_FETCH_PATH, _URL_PATH_MESSAGE)
    if "@" in parts.netloc:
        _fail(_FETCH_PATH, _URL_PATH_MESSAGE)
    if not hostname:
        _fail(_FETCH_PATH, _URL_PATH_MESSAGE)
    if port is not None and not 1 <= port <= 65535:
        _fail(_FETCH_PATH, _URL_PATH_MESSAGE)
