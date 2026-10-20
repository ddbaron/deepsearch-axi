"""Failure constructors for the frozen deepsearch-axi error table.

Every constructor returns a :class:`deepsearch_axi.models.Failure` with a fixed
message and one or two actionable hints. Callers never pass user-controlled
text except :func:`usage_failure` (safe argv tokens only) and the safe labels
accepted by :func:`setup_conflict` and :func:`local_io`.
"""

from __future__ import annotations

from typing import Literal, Tuple

from deepsearch_axi.guidance import (
    COMMANDS,
    HINT_FETCH,
    HINT_SEARCH,
    HINT_SET_KEY,
    flags_line,
)
from deepsearch_axi.models import CredentialSource, Failure, Tool

KeyConfigReason = Literal[
    "control_characters",
    "duplicate",
    "unmatched_quote",
    "malformed",
    "unreadable",
    "encoding",
    "too_large",
]

_SOURCE_LABELS = {
    "env": "DEEPSEEK_API_KEY environment variable",
    "cwd_dotenv": "./.env",
    "user_dotenv": "~/.config/deepsearch-axi/.env",
}
_TOOL_TEMPLATES = {
    "search": HINT_SEARCH,
    "fetch": HINT_FETCH,
}


def _command_name(path: Tuple[str, ...]) -> str:
    return " ".join(path) if path else "deepsearch-axi"


def _tool_template(tool: Tool) -> str:
    return _TOOL_TEMPLATES[tool]


def usage_failure(path: Tuple[str, ...], message: str) -> Failure:
    spec = COMMANDS.get(path, COMMANDS[()])
    example = spec.examples[0] if spec.examples else "deepsearch-axi --help"
    return Failure(
        code="usage",
        message=message,
        help=(f"valid flags for {_command_name(path)}: {flags_line(path)}", example),
    )


def missing_key() -> Failure:
    return Failure(
        code="missing_key",
        message=(
            "No DeepSeek API key found in DEEPSEEK_API_KEY, ./.env, "
            "or ~/.config/deepsearch-axi/.env."
        ),
        help=(
            HINT_SET_KEY,
            "or add DEEPSEEK_API_KEY=<your-deepseek-api-key> to ./.env "
            "or ~/.config/deepsearch-axi/.env",
        ),
    )


def invalid_key_config(source: CredentialSource, reason: KeyConfigReason) -> Failure:
    label = _SOURCE_LABELS[source]
    reason_words = reason.replace("_", " ")
    return Failure(
        code="invalid_config",
        message=f"DEEPSEEK_API_KEY in {label} is invalid ({reason_words}).",
        help=(
            f"fix DEEPSEEK_API_KEY in {label}: one line DEEPSEEK_API_KEY=<key>, "
            "optional export/quotes, printable ASCII key",
            HINT_SET_KEY,
        ),
    )


def invalid_base_url() -> Failure:
    return Failure(
        code="invalid_config",
        message="DEEPSEEK_BASE_URL is not a valid trusted API origin.",
        help=(
            "unset DEEPSEEK_BASE_URL to use https://api.deepseek.com",
            "export DEEPSEEK_BASE_URL=https://<trusted-host> (origin only; http only for "
            "127.0.0.1 or [::1])",
        ),
    )


def missing_executable() -> Failure:
    return Failure(
        code="invalid_config",
        message="The deepsearch-axi console script could not be resolved.",
        help=(
            "install deepsearch-axi so the deepsearch-axi console script exists, "
            "then re-run deepsearch-axi setup",
        ),
    )


def setup_conflict(path_label: str, reason: str) -> Failure:
    return Failure(
        code="invalid_config",
        message=f"Cannot safely update {path_label}: {reason}.",
        help=(f"repair or move {path_label}, then re-run deepsearch-axi setup",),
    )


def local_io(path_label: str) -> Failure:
    return Failure(
        code="local_io",
        message=f"Could not read or write {path_label}.",
        help=(f"check permissions for {path_label}, then retry",),
    )


def invalid_event(reason: str) -> Failure:
    return Failure(
        code="invalid_event",
        message=f"The native session event is invalid: {reason}.",
        help=("pipe native hook JSON with session_id and cwd inside --project on stdin",),
    )


def status_failure(status: int, tool: Tool) -> Failure:
    template = _tool_template(tool)
    message = f"DeepSeek {tool} returned HTTP {status}."
    if status == 400:
        help_items = (
            f"adjust the query or options, then retry: {template}",
            flags_line((tool,)),
        )
        code = "invalid_request"
    elif status == 401:
        code = "unauthorized"
        help_items = ("replace DEEPSEEK_API_KEY at its configured source", template)
    elif status == 402:
        code = "insufficient_balance"
        help_items = (f"top up the DeepSeek account, then retry: {template}",)
    elif status == 403:
        code = "forbidden"
        if tool == "search":
            help_items = (f"choose a permitted query: {template}",)
        else:
            help_items = (f"choose another permitted URL: {template}",)
    elif status == 404:
        code = "not_found"
        if tool == "fetch":
            help_items = ("choose another accessible URL: deepsearch-axi fetch <url>",)
        else:
            help_items = ("check DEEPSEEK_BASE_URL points at the API origin",)
    elif status in (408, 504):
        code = "timeout"
        help_items = (f"retry later: {template}",)
    elif status == 413:
        code = "request_too_large"
        help_items = (f"shorten the input, then retry: {template}",)
    elif status == 429:
        code = "rate_limited"
        help_items = (f"wait for the rate limit to clear, then retry: {template}",)
    elif 300 <= status < 400:
        code = "redirect_refused"
        message = f"DeepSeek {tool} returned an HTTP redirect; it was not followed."
        help_items = (
            "set DEEPSEEK_BASE_URL to the intended trusted API origin; "
            "redirects are never followed",
        )
    elif status >= 500:
        code = "upstream_error"
        help_items = (f"retry later: {template}",)
    else:
        code = "http_error"
        help_items = (
            f"retry: {template}",
            "check DEEPSEEK_BASE_URL if this persists",
        )
    return Failure(code=code, message=message, help=help_items, http_status=status)


def transport_failure(tool: Tool) -> Failure:
    return Failure(
        code="transport_error",
        message=f"Could not connect to the DeepSeek {tool} API.",
        help=(f"check connectivity and DEEPSEEK_BASE_URL, then retry: {_tool_template(tool)}",),
    )


def timeout_failure(tool: Tool) -> Failure:
    return Failure(
        code="timeout",
        message=f"The DeepSeek {tool} request timed out.",
        help=(f"retry later: {_tool_template(tool)}",),
    )


def response_too_large(tool: Tool) -> Failure:
    if tool == "search":
        help_items = (
            "use a narrower query or a smaller --count; --full does not raise the 8 MiB limit",
        )
    else:
        help_items = ("choose a smaller page: deepsearch-axi fetch <url>",)
    return Failure(
        code="response_too_large",
        message=f"The DeepSeek {tool} response exceeded the 8 MiB limit.",
        help=help_items,
    )


def invalid_response(tool: Tool) -> Failure:
    if tool == "search":
        help_items = (f"retry or refine: {_tool_template(tool)}",)
    else:
        help_items = ("try another URL: deepsearch-axi fetch <url>",)
    return Failure(
        code="invalid_response",
        message=f"The DeepSeek {tool} response could not satisfy the command.",
        help=help_items,
    )


def internal_error() -> Failure:
    return Failure(
        code="internal_error",
        message="An unexpected internal error occurred; the command was not completed.",
        help=("retry the command; report a bug if it persists",),
    )
