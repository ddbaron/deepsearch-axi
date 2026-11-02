"""Native Search: local validation, one POST, and a truthful ranked document.

``validate`` is pure and accepts only usage errors. ``execute`` sends exactly
one request through the shared ``PostJson`` seam, validates the requested
projection atomically, projects it in service order and returns one JSON-shaped
document for the shared output boundary.
"""

from __future__ import annotations

import math
import re
import shlex
from dataclasses import dataclass

from .contracts import (
    PROG,
    QUERY_MAX_CHARS,
    SEARCH_LIMIT_DEFAULT,
    SEARCH_LIMIT_MAX,
    SEARCH_LIMIT_MIN,
    SEARCH_PATH,
    ParsedCommand,
    PostJson,
    unusable_response_error,
    usage_error,
)

_LIMIT_PATTERN = re.compile(r"[0-9]{1,3}")

DEFAULT_FIELDS = ("title", "url", "snippet")
DEFAULT_FIELDS_WITH_CONTENT = ("title", "url", "snippet", "content")
ROW_FIELDS = (
    "title",
    "url",
    "snippet",
    "summary",
    "content",
    "date",
    "authority",
    "rerank_score",
    "site_name",
    "site_icon",
)
_REQUIRED_TEXT_FIELDS = frozenset(("title", "url", "snippet", "summary", "content"))
_OPTIONAL_TEXT_FIELDS = frozenset(("date", "site_name", "site_icon"))

_QUERY_HELP = (
    f"Pass exactly one query of 1-{QUERY_MAX_CHARS} characters with at least "
    f"one non-whitespace character; run {PROG} search --help for the rules."
)
_LIMIT_HELP = (
    f"Pass --limit N as a whole number from {SEARCH_LIMIT_MIN} to "
    f"{SEARCH_LIMIT_MAX} (default {SEARCH_LIMIT_DEFAULT}); run {PROG} search "
    "--help for the rules."
)
_FIELDS_HELP = (
    f"Use a comma-separated ordered list drawn from {', '.join(ROW_FIELDS)}; "
    f"no blanks or duplicates. Run {PROG} search --help for the rules."
)


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int
    need_content: bool
    fields: tuple[str, ...]


def _validate_query(parsed: ParsedCommand) -> str:
    query = parsed.argument
    if query is None or query == "" or not query.strip():
        raise usage_error(
            "The search query is missing, empty or only whitespace.",
            _QUERY_HELP,
        )
    if len(query) > QUERY_MAX_CHARS:
        raise usage_error(
            f"The search query is too long: it must be at most {QUERY_MAX_CHARS} characters.",
            _QUERY_HELP,
        )
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        raise usage_error(
            "The search query is not valid UTF-8 text.",
            _QUERY_HELP,
        ) from None
    return query


def _validate_limit(parsed: ParsedCommand) -> int:
    if "--limit" not in parsed.options:
        return SEARCH_LIMIT_DEFAULT
    value = parsed.options["--limit"]
    # At most three ASCII digits, so an arbitrarily long spelling never
    # reaches int().
    if not _LIMIT_PATTERN.fullmatch(value):
        raise usage_error(
            f"The --limit value must be a whole number from "
            f"{SEARCH_LIMIT_MIN} to {SEARCH_LIMIT_MAX}.",
            _LIMIT_HELP,
        )
    limit = int(value)
    if not SEARCH_LIMIT_MIN <= limit <= SEARCH_LIMIT_MAX:
        raise usage_error(
            f"The --limit value must be between {SEARCH_LIMIT_MIN} and {SEARCH_LIMIT_MAX}.",
            _LIMIT_HELP,
        )
    return limit


def _validate_fields(parsed: ParsedCommand, need_content: bool) -> tuple[str, ...]:
    if "--fields" not in parsed.options:
        return DEFAULT_FIELDS_WITH_CONTENT if need_content else DEFAULT_FIELDS
    fields = tuple(part.strip() for part in parsed.options["--fields"].split(","))
    if any(not field or field not in ROW_FIELDS for field in fields) or len(set(fields)) != len(
        fields
    ):
        raise usage_error("invalid value for --fields", _FIELDS_HELP)
    return fields


def validate(parsed: ParsedCommand) -> SearchRequest:
    """Validate one parsed Search invocation without I/O or echo of values."""
    need_content = "--need-content" in parsed.switches
    return SearchRequest(
        query=_validate_query(parsed),
        limit=_validate_limit(parsed),
        need_content=need_content,
        fields=_validate_fields(parsed, need_content),
    )


def _encode_text(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise unusable_response_error() from None
    return value


def _selected_value(item: dict[str, object], name: str) -> object:
    value = item.get(name)
    if name in _REQUIRED_TEXT_FIELDS:
        if not isinstance(value, str):
            raise unusable_response_error()
        return _encode_text(value)
    if name in _OPTIONAL_TEXT_FIELDS:
        if value is not None and not isinstance(value, str):
            raise unusable_response_error()
        return None if value is None else _encode_text(value)
    if name == "authority":
        if isinstance(value, bool) or not isinstance(value, int):
            raise unusable_response_error()
        if not -1 <= value <= 4:
            raise unusable_response_error()
        return value
    # rerank_score
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise unusable_response_error()
    if isinstance(value, float) and not math.isfinite(value):
        raise unusable_response_error()
    if not 0 <= value <= 3:
        raise unusable_response_error()
    return value


def _selected_rows(value: object, limit: int, fields: tuple[str, ...]) -> list[dict[str, object]]:
    """Validate the requested fields atomically and preserve their order."""
    if not isinstance(value, dict):
        raise unusable_response_error()
    results = value.get("results")
    if not isinstance(results, list):
        raise unusable_response_error()

    rows: list[dict[str, object]] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            raise unusable_response_error()
        rows.append({name: _selected_value(item, name) for name in fields})
    return rows


def _fetch_hint() -> str:
    return f"Run `{PROG} fetch <url>` to read a result page's text with the returned URL."


def _higher_limit_hint(query: str, limit: int) -> str:
    next_limit = min(limit * 2, SEARCH_LIMIT_MAX)
    template = f"{PROG} search --limit {next_limit} -- {shlex.quote(query)}"
    return f"Run `{template}` to ask for a higher limit; more results may exist."


def _empty_document() -> dict[str, object]:
    return {
        "count": 0,
        "results": [],
        "message": "No results matched the query.",
        "help": [f'Try a broader or different query: {PROG} search "<broader query>"'],
    }


def execute(request: SearchRequest, post: PostJson) -> dict[str, object]:
    """Make exactly one native Search request and return one document."""
    payload: dict[str, object] = {
        "query": request.query,
        "count": request.limit,
    }
    if request.need_content:
        payload["need_content"] = True
    value = post(SEARCH_PATH, payload)
    rows = _selected_rows(value, request.limit, request.fields)
    if not rows:
        return _empty_document()

    help_lines = [_fetch_hint()]
    if len(rows) == request.limit and request.limit < SEARCH_LIMIT_MAX:
        help_lines.append(_higher_limit_hint(request.query, request.limit))
    return {
        "count": len(rows),
        "results": rows,
        "help": help_lines,
    }
