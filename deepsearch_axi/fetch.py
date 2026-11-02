"""Native Fetch: validate one URL, POST once, and render honest page text.

``validate`` never touches the network and never echoes the URL, so an
embedded password cannot be reflected. ``execute`` calls the injected
``post`` exactly once and validates the service answer atomically before any
output exists.
"""

from __future__ import annotations

import shlex
import unicodedata
import urllib.parse
from dataclasses import dataclass

from .contracts import (
    FETCH_PATH,
    FETCH_PREVIEW_CHARS,
    URL_MAX_CHARS,
    CliError,
    ParsedCommand,
    PostJson,
    unusable_response_error,
    usage_error,
)

_HELP = "Run `deepsearch-axi fetch --help` for the URL rules and examples."


@dataclass(frozen=True)
class FetchRequest:
    """One validated Fetch invocation ready for the shared transport."""

    url: str
    full: bool


def _invalid(message: str) -> CliError:
    return usage_error(message, _HELP)


def _has_whitespace_or_controls(value: str) -> bool:
    return any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value)


def validate(parsed: ParsedCommand) -> FetchRequest:
    """Check the positional URL locally and keep it byte-for-byte unchanged."""
    url = parsed.argument
    if url is None or url == "":
        raise _invalid("The URL is empty: fetch needs one nonempty http or https URL.")
    if len(url) > URL_MAX_CHARS:
        raise _invalid(f"The URL is too long: at most {URL_MAX_CHARS} characters are accepted.")
    if _has_whitespace_or_controls(url):
        raise _invalid(
            "The URL contains whitespace or control characters; it must be "
            "one unbroken http(s) URL."
        )
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid("The URL cannot be represented as UTF-8 text.") from None

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise _invalid("The URL could not be parsed as an http(s) URL.") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise _invalid("The URL scheme must be http or https.")
    if not parts.netloc:
        raise _invalid("The URL must include a host.")
    if "@" in parts.netloc:
        raise _invalid("The URL must not contain a username or password.")
    if not parts.hostname:
        raise _invalid("The URL must include a host.")

    return FetchRequest(url=url, full="--full" in parsed.switches)


def _page_text(value: object) -> tuple[str, str]:
    """Return the service title and content, or fail atomically."""
    if not isinstance(value, dict):
        raise unusable_response_error()
    title = value.get("title")
    content = value.get("content")
    if not isinstance(title, str) or not isinstance(content, str):
        raise unusable_response_error()
    try:
        title.encode("utf-8")
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise unusable_response_error() from None
    return title, content


def execute(request: FetchRequest, post: PostJson) -> dict[str, object]:
    """Fetch page text once and render a deterministic preview or full view."""
    title, content = _page_text(post(FETCH_PATH, {"url": request.url}))

    length = len(content)
    truncated = not request.full and length > FETCH_PREVIEW_CHARS
    shown = content[:FETCH_PREVIEW_CHARS] if truncated else content

    document: dict[str, object] = {
        "url": request.url,
        "title": title,
        "length": length,
        "content": shown,
    }
    if truncated:
        document["truncated"] = True
        document["help"] = (
            f"Run `deepsearch-axi fetch {shlex.quote(request.url)} --full` "
            f"for all {length} characters."
        )
    elif content == "":
        document["status"] = "No page text was returned for this URL."
    return document
