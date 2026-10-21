"""Hints emitted by documents must parse back through the production argv parser.

Hint text is copied verbatim by agents. Each scenario builds a real search or
fetch document, substitutes the documented placeholders with concrete values,
and feeds every hint back through ``deepsearch_axi.argv.parse_args``. This pins
the seam between ``deepsearch_axi.documents`` (hint builders) and
``deepsearch_axi.argv`` (flag grammar), both of which derive from
``deepsearch_axi.guidance``.
"""

from __future__ import annotations

import shlex

import pytest

from deepsearch_axi.argv import parse_args
from deepsearch_axi.documents import fetch_document, search_document
from deepsearch_axi.guidance import COMMANDS, HINT_FETCH_FULL
from deepsearch_axi.models import (
    DEFAULT_FIELDS,
    DEFAULT_FIELDS_WITH_CONTENT,
    DeepsearchError,
    FetchCommand,
    OutputOptions,
    SearchCommand,
    SearchOptions,
)

PLACEHOLDERS = {
    "<refined-query>": "rust async runtime",
    "<query>": "rust async runtime",
    "<original-question>": "why is it fast",
    "<ip>": "203.0.113.7",
    "<domain>": "docs.python.org",
    "<url>": "https://example.com/page",
}


def _row(snippet: str = "short snippet") -> dict:
    return {
        "title": "Title",
        "url": "https://example.com/a",
        "snippet": snippet,
        "summary": "summary text",
        "content": "page content",
        "date": "2025-01-02",
        "authority": 1,
        "rerank_score": 2,
        "site_name": "Example",
        "site_icon": None,
    }


def _concrete(hint: str) -> str:
    for placeholder, value in PLACEHOLDERS.items():
        hint = hint.replace(placeholder, value)
    assert "<" not in hint and ">" not in hint, f"unreplaced placeholder in hint {hint!r}"
    return hint


def _parse_hint(hint: str):
    tokens = shlex.split(_concrete(hint))
    assert tokens[0] == "deepsearch-axi", f"hint does not start with the binary: {hint!r}"
    return parse_args(tokens[1:])


SCENARIOS = {
    "default_projection": (
        SearchOptions(query="q"),
        OutputOptions(),
        [_row()],
    ),
    "content_optin_projection": (
        SearchOptions(query="q"),
        OutputOptions(fields=("title", "url", "snippet", "content")),
        [_row()],
    ),
    "explicit_projection_without_content": (
        SearchOptions(query="q"),
        OutputOptions(fields=("title", "url", "summary")),
        [_row()],
    ),
    "need_content_default_projection": (
        SearchOptions(query="q", need_content=True),
        OutputOptions(fields=DEFAULT_FIELDS_WITH_CONTENT),
        [_row()],
    ),
    "ori_question_localization_count": (
        SearchOptions(
            query="q",
            count=5,
            ori_question="why is it fast",
            localization="203.0.113.7",
        ),
        OutputOptions(),
        [_row()],
    ),
    "truncated_default_projection": (
        SearchOptions(query="q"),
        OutputOptions(),
        [_row(snippet="x" * 250)],
    ),
    "empty_results": (
        SearchOptions(query="q", count=3),
        OutputOptions(),
        [],
    ),
    # Reachable from the CLI as `search q --need-content --fields title,url,snippet`:
    # the explicit projection equals the default and must not be emitted twice.
    "need_content_with_explicit_default_fields": (
        SearchOptions(query="q", need_content=True),
        OutputOptions(fields=DEFAULT_FIELDS),
        [_row()],
    ),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_search_hints_parse_back_through_argv(scenario: str) -> None:
    request, output, rows = SCENARIOS[scenario]
    document = search_document("q", rows, output, request=request)
    assert 1 <= len(document["help"]) <= 2, f"{scenario}: expected one or two hints"
    for hint in document["help"]:
        assert hint.count("--fields") <= 1, f"{scenario}: repeated --fields in hint {hint!r}"
        try:
            command = _parse_hint(hint)
        except DeepsearchError as error:
            pytest.fail(
                f"{scenario}: hint {hint!r} is rejected by parse_args: {error.failure.message}"
            )
        assert isinstance(command, (SearchCommand, FetchCommand)), (
            f"{scenario}: hint {hint!r} parsed to unexpected {type(command).__name__}"
        )


def test_search_hint_parsed_options_match_the_request() -> None:
    """The reparsed hint must carry the same count/content/projection it advertises."""
    request = SearchOptions(query="q", count=7, need_content=True)
    output = OutputOptions(fields=("title", "url", "snippet", "content"))
    rows = [_row(snippet="y" * 250)]
    document = search_document("q", rows, output, request=request)
    truncated_hint = document["help"][1]
    command = _parse_hint(truncated_hint)
    assert isinstance(command, SearchCommand), f"expected SearchCommand, got {command!r}"
    assert command.options.count == 7, f"count drifted through hint: {truncated_hint!r}"
    assert command.options.need_content is True, f"need_content lost: {truncated_hint!r}"
    assert command.output.fields == output.fields, (
        f"projection drifted through hint: {truncated_hint!r} -> {command.output.fields}"
    )
    assert command.output.full is True, f"truncated hint must carry --full: {truncated_hint!r}"


def test_fetch_full_hint_parses_as_full_fetch() -> None:
    command = _parse_hint(HINT_FETCH_FULL)
    assert isinstance(command, FetchCommand), f"expected FetchCommand, got {command!r}"
    assert command.full is True, "fetch --full hint must parse with full=True"


def test_fetch_document_truncation_hint_parses_as_full_fetch() -> None:
    page = {"title": "T", "content": "z" * 1500}
    document = fetch_document("https://example.com/page", page, full=False)
    assert document["help"] == [HINT_FETCH_FULL]
    command = _parse_hint(document["help"][0])
    assert isinstance(command, FetchCommand) and command.full is True, (
        "fetch truncation hint must reparse to a full fetch"
    )


def test_every_documented_search_flag_is_accepted_by_argv() -> None:
    """Guidance flag table and parser grammar must agree for each search flag."""
    samples = {
        "--count": "5",
        "--need-content": None,
        "--ori-question": "why",
        "--localization": "::1",
        "--fields": "title,url",
        "--full": None,
    }
    spec_flags = {flag.name for flag in COMMANDS[("search",)].flags}
    assert spec_flags == set(samples), (
        f"guidance search flags drifted from the test sample table: {sorted(spec_flags)}"
    )
    for name, value in samples.items():
        argv = ["search", "q", name] if value is None else ["search", "q", name, value]
        command = parse_args(argv)
        assert isinstance(command, SearchCommand), f"{name} did not parse as a search command"
