"""Pure JSON-shaped output documents for every user-visible deepsearch-axi view.

Documents contain only JSON-compatible values; nothing here encodes TOON or
writes to a stream. Redaction happens before truncation so the reported
``total`` always counts the redacted value, and :mod:`deepsearch_axi.render`
repeats redaction as defense in depth.

Search hint rules (at most two hints):

* empty results: one query-refinement template;
* otherwise the first hint is ``fetch <url>``, replaced by the explicit
  ``--need-content`` opt-in when content was projected without requesting it;
* the second hint is the ``--full`` template when text was truncated, a field
  expansion template for the default projection, or a ``site:<domain>``
  refinement template otherwise.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from deepsearch_axi.failures import invalid_response
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
    REDACTED,
    TEXT_LIMITS,
    Activity,
    AgentName,
    DeepsearchError,
    Failure,
    JsonObject,
    JsonValue,
    LocalContext,
    OutputOptions,
    PriorEvent,
    SearchOptions,
    SessionEventKind,
    SetupResult,
)

EMPTY_SEARCH_MESSAGE = "No results returned for this query."
_INVALID_KEY_HELP = "fix DEEPSEEK_API_KEY at its source; run deepsearch-axi for status"
_INVALID_ORIGIN_HELP = "unset DEEPSEEK_BASE_URL or set an https origin"
_START_SESSION_HELP = "start a new agent session to load deepsearch-axi context"
_FIELD_EXPANSION = "--fields title,url,snippet,summary,date"


def _redact_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact(value: JsonValue, secrets: tuple[str, ...]) -> JsonValue:
    """Replace every non-empty secret occurrence in strings, including keys."""
    if not secrets:
        return value
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            (_redact_text(key, secrets) if isinstance(key, str) else key): redact(item, secrets)
            for key, item in value.items()
        }
    return value


def _truncate(
    value: JsonValue,
    field: str,
    path: str,
    truncated: list[JsonValue],
    *,
    full: bool,
) -> JsonValue:
    """Cut a recognized text field to its preview limit, recording metadata."""
    limit = TEXT_LIMITS.get(field)
    if full or limit is None or not isinstance(value, str) or len(value) <= limit:
        return value
    emitted = value[:limit]
    truncated.append({"path": path, "shown": len(emitted), "total": len(value)})
    return emitted


def search_flags_template(request: SearchOptions, output: OutputOptions, *, full: bool) -> str:
    """Render request options for a hint, substituting placeholders for runtime values."""
    parts = [f"--count {request.count}"]
    if request.need_content:
        parts.append("--need-content")
    if request.ori_question is not None:
        parts.append('--ori-question "<original-question>"')
    if request.localization:
        parts.append('--localization "<ip>"')
    default_fields = DEFAULT_FIELDS_WITH_CONTENT if request.need_content else DEFAULT_FIELDS
    if tuple(output.fields) != default_fields:
        parts.append("--fields " + ",".join(output.fields))
    if full:
        parts.append("--full")
    return "".join(f" {part}" for part in parts)


def _search_help(
    request: SearchOptions, output: OutputOptions, *, empty: bool, truncated: bool
) -> list[str]:
    flags = search_flags_template(request, output, full=False)
    if empty:
        return [f'deepsearch-axi search "<refined-query>"{flags}']
    content_optin = "content" in output.fields and not request.need_content
    if content_optin:
        opted_in = replace(request, need_content=True)
        first = 'deepsearch-axi search "<query>"' + search_flags_template(
            opted_in, output, full=False
        )
    else:
        first = HINT_FETCH
    if truncated:
        second = 'deepsearch-axi search "<query>"' + search_flags_template(
            request, output, full=True
        )
    elif tuple(output.fields) == DEFAULT_FIELDS:
        second = 'deepsearch-axi search "<query>"' + flags + " " + _FIELD_EXPANSION
    else:
        second = 'deepsearch-axi search "<query> site:<domain>"' + flags
    return [first, second]


def search_document(
    query: str,
    results: list[JsonObject],
    options: OutputOptions,
    *,
    request: SearchOptions | None = None,
    secrets: tuple[str, ...] = (),
) -> JsonObject:
    """Build the search document from validated response rows in server order."""
    request = SearchOptions(query=query) if request is None else request
    rows: list[JsonValue] = []
    truncated: list[JsonValue] = []
    for index, raw_row in enumerate(results):
        row = cast(JsonObject, redact(raw_row, secrets))
        emitted: JsonObject = {}
        for field in options.fields:
            value = row.get(field)
            emitted[field] = _truncate(
                value,
                field,
                f"results[{index}].{field}",
                truncated,
                full=options.full,
            )
        rows.append(emitted)

    document: JsonObject = {
        "query": redact(query, secrets),
        "count": len(results),
        "total": len(results),
        "total_scope": "response",
    }
    if not results:
        document["message"] = EMPTY_SEARCH_MESSAGE
    document["results"] = rows
    if truncated:
        document["truncated"] = truncated
    document["help"] = _search_help(request, options, empty=not results, truncated=bool(truncated))
    return document


def fetch_document(
    url: str,
    page: dict[str, str],
    full: bool = False,
    *,
    secrets: tuple[str, ...] = (),
) -> JsonObject:
    """Build the fetch detail document, preserving Markdown and newlines exactly."""
    content = page.get("content")
    title = page.get("title", "")
    if not isinstance(content, str) or content.strip() == "":
        raise DeepsearchError(invalid_response("fetch"))

    truncated: list[JsonValue] = []
    emitted_content = _truncate(
        redact(content, secrets), "content", "page.content", truncated, full=full
    )
    emitted_title = _truncate(redact(title, secrets), "title", "page.title", truncated, full=full)
    document: JsonObject = {
        "page": {
            "url": redact(url, secrets),
            "title": emitted_title,
            "content": emitted_content,
        }
    }
    if truncated:
        document["truncated"] = truncated
        document["help"] = [HINT_FETCH_FULL]
    return document


def error_document(failure: Failure) -> JsonObject:
    """Build the decoded error shape ``{error, help}`` from a safe Failure."""
    error: JsonObject = {"code": failure.code, "message": failure.message}
    if failure.http_status is not None:
        error["http_status"] = failure.http_status
    return {"error": error, "help": list(failure.help)}


def _flag_label(name: str, metavar: str | None) -> str:
    return name if metavar is None else f"{name} {metavar}"


def help_document(path: tuple[str, ...]) -> JsonObject:
    """Build spec-driven help for a command path; contains no live values."""
    spec = COMMANDS.get(path, COMMANDS[()])
    command = "deepsearch-axi" if not path else "deepsearch-axi " + " ".join(path)
    document: JsonObject = {
        "command": command,
        "description": spec.summary,
        "usage": spec.usage,
    }
    if spec.arguments:
        document["arguments"] = [
            {"name": argument.name, "description": argument.description}
            for argument in spec.arguments
        ]
    if spec.subcommands:
        document["commands"] = [
            {"name": name, "description": COMMANDS[spec.path + (name,)].summary}
            for name in spec.subcommands
        ]
    flags: list[JsonValue] = [
        {
            "flag": _flag_label(flag.name, flag.metavar),
            "default": flag.default,
            "description": flag.description,
        }
        for flag in spec.flags
    ]
    flags.append({"flag": "--help", "default": "false", "description": "show help and exit"})
    document["flags"] = flags
    document["examples"] = list(spec.examples)
    return document


def _prior_value(prior: PriorEvent) -> JsonObject:
    return {"agent": prior.agent, "event": prior.event, "sessions": prior.sessions}


def _activity_value(activity: Activity) -> JsonObject:
    return {
        "search_ok": activity.search_ok,
        "search_fail": activity.search_fail,
        "fetch_ok": activity.fetch_ok,
        "fetch_fail": activity.fetch_fail,
    }


def home_document(context: LocalContext) -> JsonObject:
    """Build the no-argument live readiness document."""
    if context.origin_state == "invalid":
        first_help = _INVALID_ORIGIN_HELP
    elif context.auth.state == "missing":
        first_help = HINT_SET_KEY
    elif context.auth.state == "invalid":
        first_help = _INVALID_KEY_HELP
    else:
        first_help = HINT_SEARCH
    any_integration_missing = any(state != "installed" for _, state in context.integrations)
    help_items = [first_help, HINT_SETUP if any_integration_missing else HINT_FETCH]

    document: JsonObject = {
        "bin": context.bin,
        "description": DESCRIPTION,
        "cwd": context.cwd,
        "auth": {
            "state": context.auth.state,
            "source": context.auth.source,
            "verified": False,
        },
        "api": {"origin": context.origin, "state": context.origin_state},
        "integrations": {agent: state for agent, state in context.integrations},
    }
    if context.prior is not None:
        document["prior"] = _prior_value(context.prior)
    if context.activity is not None:
        document["activity"] = _activity_value(context.activity)
    document["help"] = help_items
    return document


def ambient_document(context: LocalContext, agent: AgentName) -> JsonObject:
    """Build the compact session-start context for ``agent``."""
    integrations = dict(context.integrations)
    document: JsonObject = {
        "bin": context.bin,
        "description": DESCRIPTION,
        "cwd": context.cwd,
        "auth": {"state": context.auth.state},
        "api": {"state": context.origin_state},
        "integration": integrations[agent],
    }
    if context.prior is not None:
        document["prior"] = _prior_value(context.prior)
    if context.activity is not None:
        document["activity"] = _activity_value(context.activity)
    if context.auth.state == "configured":
        document["help"] = [HINT_SEARCH, HINT_FETCH]
    else:
        document["help"] = [HINT_SET_KEY, HINT_SEARCH]
    return document


def setup_document(result: SetupResult, project_display: str) -> JsonObject:
    """Build the explicit setup result document with uniform host rows."""
    return {
        "setup": {"project": project_display, "changed": bool(result.changed)},
        "hosts": [
            {
                "agent": outcome.agent,
                "status": outcome.status,
                "files": ";".join(outcome.files),
            }
            for outcome in result.outcomes
        ],
        "help": [_START_SESSION_HELP, HINT_SEARCH],
    }


def session_end_document(kind: SessionEventKind, recorded: bool) -> JsonObject:
    """Build the lifecycle capture confirmation document."""
    return {"session": {"event": kind, "recorded": bool(recorded)}}


def collapse_home(path: str, home: str) -> str:
    """Collapse an actual HOME prefix in an absolute path to ``~``."""
    if not home:
        return path
    base = home.rstrip("/")
    prefix = base + "/"
    if path == base or path == prefix:
        return "~"
    if base and path.startswith(prefix):
        return "~/" + path[len(prefix) :]
    return path
