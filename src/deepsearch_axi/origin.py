"""Validation of the ``DEEPSEEK_BASE_URL`` API origin override.

The override selects where credentials are sent, so the accepted grammar is
deliberately narrow: an HTTPS origin (or numeric-loopback HTTP for local
stubs), never a path prefix, query, fragment, or userinfo.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from deepsearch_axi.failures import invalid_base_url
from deepsearch_axi.models import OFFICIAL_ORIGIN, DeepsearchError

# RFC 3986 reg-name characters, minus the pct-encoded escapes the override
# policy never needs; bare IPv6 literals use hex digits, colons, and dots.
_REG_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.")
_IP_LITERAL_CHARS = frozenset("0123456789abcdefABCDEF:.")


def _has_valid_host(hostname: str) -> bool:
    allowed = _IP_LITERAL_CHARS if ":" in hostname else _REG_NAME_CHARS
    return all(char in allowed for char in hostname)


def _has_forbidden_code(value: str) -> bool:
    for char in value:
        code = ord(char)
        if char.isspace() or char in "?#@":
            return True
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            return True
        if 0xD800 <= code <= 0xDFFF:
            return True
    return False


def validate_base_url(value: str | None) -> str:
    """Return the normalized origin, or raise ``invalid_config`` for an unsafe value."""
    if value is None or value == "":
        return OFFICIAL_ORIGIN
    if _has_forbidden_code(value):
        raise DeepsearchError(invalid_base_url())
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise DeepsearchError(invalid_base_url()) from None
    scheme = parts.scheme
    if scheme not in ("http", "https"):
        raise DeepsearchError(invalid_base_url())
    if not parts.netloc or not hostname:
        raise DeepsearchError(invalid_base_url())
    if not _has_valid_host(hostname):
        raise DeepsearchError(invalid_base_url())
    if port is not None and not 1 <= port <= 65535:
        raise DeepsearchError(invalid_base_url())
    if parts.path not in ("", "/"):
        raise DeepsearchError(invalid_base_url())
    if scheme == "http" and hostname not in {"127.0.0.1", "::1"}:
        raise DeepsearchError(invalid_base_url())
    return f"{scheme}://{parts.netloc}"
