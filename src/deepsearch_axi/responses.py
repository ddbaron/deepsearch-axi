"""Schema validation and projection for 200 responses from the tools API.

Validators return fresh dicts containing only the documented keys, so unknown
server fields can never reach documents, rendering, hints or logs.
"""

from __future__ import annotations

import math
from typing import NoReturn

from deepsearch_axi.failures import invalid_response
from deepsearch_axi.models import SEARCH_FIELDS, DeepsearchError, JsonObject, Tool

_REQUIRED_TEXT_FIELDS = ("title", "url", "snippet", "summary", "content")
_OPTIONAL_TEXT_FIELDS = ("date", "site_name", "site_icon")


def _invalid(tool: Tool) -> NoReturn:
    raise DeepsearchError(invalid_response(tool))


def validate_search_response(payload: JsonObject, requested_count: int) -> list[JsonObject]:
    """Validate and project a search payload to the ten known fields."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) > requested_count:
        _invalid("search")

    validated: list[JsonObject] = []
    for item in results:
        if not isinstance(item, dict):
            _invalid("search")
        for name in _REQUIRED_TEXT_FIELDS:
            if not isinstance(item.get(name), str):
                _invalid("search")
        for name in _OPTIONAL_TEXT_FIELDS:
            value = item.get(name)
            if value is not None and not isinstance(value, str):
                _invalid("search")
        authority = item.get("authority")
        if isinstance(authority, bool) or not isinstance(authority, int):
            _invalid("search")
        if not -1 <= authority <= 4:
            _invalid("search")
        rerank_score = item.get("rerank_score")
        if isinstance(rerank_score, bool) or not isinstance(rerank_score, (int, float)):
            _invalid("search")
        if isinstance(rerank_score, float) and not math.isfinite(rerank_score):
            _invalid("search")
        if not 0 <= rerank_score <= 3:
            _invalid("search")
        validated.append({name: item.get(name) for name in SEARCH_FIELDS})
    return validated


def validate_fetch_response(payload: JsonObject) -> dict[str, str]:
    """Validate and project a fetch payload to ``title`` and ``content``."""
    if not isinstance(payload, dict):
        _invalid("fetch")
    title = payload.get("title")
    content = payload.get("content")
    if not isinstance(title, str) or not isinstance(content, str):
        _invalid("fetch")
    if content.strip() == "":
        _invalid("fetch")
    return {"title": title, "content": content}


__all__ = [
    "validate_fetch_response",
    "validate_search_response",
]
