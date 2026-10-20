"""Spec-driven, abbreviation-free argv parsing into frozen command models.

The parser is built from :data:`deepsearch_axi.guidance.COMMANDS`, accepts only
exact long-flag names, and performs no I/O: every rejection is a ``usage``
failure raised before any credential file is read.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

from deepsearch_axi import validation
from deepsearch_axi.failures import usage_failure
from deepsearch_axi.guidance import COMMANDS
from deepsearch_axi.models import (
    DEFAULT_FIELDS,
    DEFAULT_FIELDS_WITH_CONTENT,
    SEARCH_DEFAULT_COUNT,
    DeepsearchError,
    FetchCommand,
    HelpCommand,
    HomeCommand,
    OutputOptions,
    ParsedCommand,
    SearchCommand,
    SearchOptions,
    SessionCommand,
    SetupCommand,
)

_VERSION_ALIASES = frozenset({"-v", "-V", "--version"})
_VERSION_MESSAGE = "--version is only valid alone: deepseek-axi --version"
_SAFE_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
_UNRECOGNIZED = "an unrecognized argument"
_SESSION_EVENTS = ("start", "end")
_LEAF_COMMANDS = ("search", "fetch", "setup")


def _usage(path: tuple[str, ...], message: str) -> DeepsearchError:
    return DeepsearchError(usage_failure(path, message))


def _command_label(path: tuple[str, ...]) -> str:
    return " ".join(path) if path else "deepsearch-axi"


def _safe_token(token: str) -> str:
    name = token.split("=", 1)[0]
    return name if _SAFE_FLAG_RE.fullmatch(name) else _UNRECOGNIZED


def _is_flag_token(token: str) -> bool:
    return len(token) > 1 and token.startswith("-")


def _is_version_alias(token: str) -> bool:
    return token.split("=", 1)[0] in _VERSION_ALIASES


def _mark_seen(path: tuple[str, ...], seen: set[str], name: str) -> None:
    if name in seen:
        raise _usage(path, f"repeated flag {name} for {_command_label(path)}")
    seen.add(name)


def _check_choice(path: tuple[str, ...], name: str, value: str) -> None:
    spec = COMMANDS[path]
    for flag in spec.flags:
        if flag.name == name:
            if flag.choices and value not in flag.choices:
                raise _usage(path, f"invalid value for {name}")
            return


def parse_args(argv: Sequence[str]) -> ParsedCommand:
    """Parse ``argv`` (without the program name) into a frozen command model."""
    tokens = list(argv)
    if not tokens:
        return HomeCommand()
    first = tokens[0]
    if _is_version_alias(first):
        raise _usage((), _VERSION_MESSAGE)
    if first == "session":
        return _parse_session(tokens[1:])
    if first in _LEAF_COMMANDS:
        return _parse_tokens((first,), tokens[1:])
    if first == "--help":
        return _parse_tokens((), tokens)
    raise _usage((), f"unknown command or flag {_safe_token(first)}")


def _parse_session(rest: Sequence[str]) -> ParsedCommand:
    path = ("session",)
    if not rest:
        raise _usage(path, "missing <start|end> for session")
    second = rest[0]
    if second in _SESSION_EVENTS:
        return _parse_tokens(("session", second), rest[1:])
    if second == "--help":
        return _parse_tokens(path, rest)
    if _is_version_alias(second):
        raise _usage(path, _VERSION_MESSAGE)
    if _is_flag_token(second):
        raise _usage(path, f"unknown flag {_safe_token(second)} for session")
    raise _usage(path, "unknown subcommand for session")


def _parse_tokens(path: tuple[str, ...], tokens: Sequence[str]) -> ParsedCommand:
    spec = COMMANDS[path]
    specs = {flag.name: flag for flag in spec.flags}
    allowed_positionals = len(spec.arguments)
    values: dict[str, str] = {}
    switches: set[str] = set()
    positionals: list[str] = []
    seen: set[str] = set()
    help_requested = False
    end_of_flags = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not end_of_flags and token == "--":
            end_of_flags = True
            continue
        if not end_of_flags and _is_flag_token(token):
            head, separator, inline = token.partition("=")
            if _is_version_alias(token):
                raise _usage(path, _VERSION_MESSAGE)
            if head == "--help":
                if separator:
                    raise _usage(path, "invalid value for --help")
                _mark_seen(path, seen, head)
                help_requested = True
                continue
            flag = specs.get(head)
            if flag is None:
                raise _usage(
                    path,
                    f"unknown flag {_safe_token(token)} for {_command_label(path)}",
                )
            _mark_seen(path, seen, head)
            if flag.metavar is None:
                if separator:
                    raise _usage(path, f"invalid value for {head}")
                switches.add(head)
                continue
            if separator:
                values[head] = inline
                continue
            if index >= len(tokens):
                raise _usage(path, f"missing value for {head}")
            candidate = tokens[index]
            if _is_version_alias(candidate):
                raise _usage(path, _VERSION_MESSAGE)
            if _is_flag_token(candidate):
                raise _usage(path, f"missing value for {head}")
            values[head] = candidate
            index += 1
            continue
        positionals.append(token)
    if help_requested:
        if len(positionals) <= allowed_positionals:
            return HelpCommand(path)
        raise _usage(path, f"too many arguments for {_command_label(path)}")
    if len(positionals) < allowed_positionals:
        missing = spec.arguments[len(positionals)]
        raise _usage(path, f"missing <{missing.name}> for {_command_label(path)}")
    if len(positionals) > allowed_positionals:
        raise _usage(path, f"too many arguments for {_command_label(path)}")
    return _build(path, values, switches, positionals)


def _build(
    path: tuple[str, ...],
    values: dict[str, str],
    switches: set[str],
    positionals: list[str],
) -> ParsedCommand:
    if path == ("search",):
        return _build_search(values, switches, positionals[0])
    if path == ("fetch",):
        url = positionals[0]
        validation.validate_fetch_url(url)
        return FetchCommand(url=url, full="--full" in switches)
    if path == ("setup",):
        agent = values.get("--agent", "all")
        _check_choice(path, "--agent", agent)
        return SetupCommand(agent=agent)
    if path in (("session", "start"), ("session", "end")):
        return _build_session(path, values)
    raise _usage(path, "unknown command")


def _build_search(values: dict[str, str], switches: set[str], query: str) -> SearchCommand:
    validation.validate_query(query)
    count = (
        validation.parse_count(values["--count"]) if "--count" in values else SEARCH_DEFAULT_COUNT
    )
    need_content = "--need-content" in switches
    ori_question = values.get("--ori-question")
    if ori_question is not None:
        validation.validate_ori_question(ori_question)
    localization = values.get("--localization", "")
    validation.validate_localization(localization)
    if "--fields" in values:
        fields = validation.parse_fields(values["--fields"])
    elif need_content:
        fields = DEFAULT_FIELDS_WITH_CONTENT
    else:
        fields = DEFAULT_FIELDS
    return SearchCommand(
        options=SearchOptions(
            query=query,
            count=count,
            need_content=need_content,
            ori_question=ori_question,
            localization=localization,
        ),
        output=OutputOptions(fields=fields, full="--full" in switches),
    )


def _build_session(path: tuple[str, ...], values: dict[str, str]) -> SessionCommand:
    for required in ("--agent", "--project"):
        if required not in values:
            raise _usage(path, f"missing {required} for {_command_label(path)}")
    agent = values["--agent"]
    _check_choice(path, "--agent", agent)
    project = values["--project"]
    if not os.path.isabs(project):
        raise _usage(path, "invalid value for --project")
    return SessionCommand(event=path[1], agent=agent, project=Path(project))
