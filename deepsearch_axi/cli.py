"""Command-line grammar, discovery documents, dispatch and the output boundary.

``main`` is the only place that writes to stdout and picks the exit code, so
the shared TOON/error contract lives in one module.
"""

from __future__ import annotations

import functools
import importlib
import importlib.metadata
import os
import sys
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config, output, transport
from .contracts import (
    API_KEY_ENV,
    BACKEND_ENV,
    DIST_NAME,
    FETCH_PREVIEW_CHARS,
    PROG,
    QUERY_MAX_CHARS,
    SEARCH_LIMIT_DEFAULT,
    SEARCH_LIMIT_MAX,
    SEARCH_LIMIT_MIN,
    URL_MAX_CHARS,
    CliError,
    ExitCode,
    ParsedCommand,
    usage_error,
)

_VERSION_FLAGS = ("--version", "-v", "-V")
_ROOT_FLAGS = ("--help", "--version", "-v", "-V")
_COMMANDS = ("search", "fetch", "doctor")

_VALUE_FLAGS: dict[str, tuple[str, ...]] = {
    "search": ("--limit", "--fields"),
    "fetch": (),
    "doctor": (),
}
_SWITCH_FLAGS: dict[str, frozenset[str]] = {
    "search": frozenset({"--need-content"}),
    "fetch": frozenset({"--full"}),
    "doctor": frozenset(),
}
_ARITY: dict[str, int] = {"search": 1, "fetch": 1, "doctor": 0}
_ROLE: dict[str, str] = {"search": "query", "fetch": "url"}
_USAGE: dict[str, str] = {
    "search": f"{PROG} search [--limit N] [--need-content] [--fields FIELD,...] [--] <query>",
    "fetch": f"{PROG} fetch [--full] [--] <url>",
    "doctor": f"{PROG} doctor",
}
_FLAG_SUMMARY: dict[str, tuple[str, ...]] = {
    "search": (
        (f"--limit N ({SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX}, default {SEARCH_LIMIT_DEFAULT})"),
        "--need-content",
        "--fields FIELD,...",
        "--help",
    ),
    "fetch": ("--full", "--help"),
    "doctor": ("--help",),
}
_POSITIONAL_SUMMARY: dict[str, str] = {
    "search": "one query positional argument",
    "fetch": "one url positional argument",
    "doctor": "no positional arguments",
}


@dataclass(frozen=True)
class _Command:
    name: str
    summary: str
    argument: str | None


_COMMAND_INFO: dict[str, _Command] = {
    "search": _Command(
        "search",
        "search the web through DeepSeek's native Search API",
        f"query: one search query of 1-{QUERY_MAX_CHARS} characters with at "
        "least one non-whitespace character",
    ),
    "fetch": _Command(
        "fetch",
        "fetch a page's text through DeepSeek's native Fetch API",
        f"url: one http or https URL with a host and no username or password, "
        f"at most {URL_MAX_CHARS} characters",
    ),
    "doctor": _Command(
        "doctor",
        "report local credential presence and the effective backend, without a network request",
        None,
    ),
}


# ---------------------------------------------------------------------------
# Grammar helpers


def _is_option_token(token: str) -> bool:
    """Any token starting with '-' except the bare '-' is an option token."""
    return token.startswith("-") and token != "-"


def _option_name(token: str) -> str:
    return token.split("=", 1)[0]


def _printable(value: str) -> str:
    """Keep echoed argv text representable in the one UTF-8 document."""
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


def _valid_flags_text(name: str) -> str:
    return ", ".join(_FLAG_SUMMARY[name])


def _flag_help(name: str) -> str:
    return f"Valid flags for {name}: {_valid_flags_text(name)}; usage: {_USAGE[name]}"


def _unknown_flag_error(name: str | None, flag: str) -> CliError:
    flag = _printable(flag)
    if name is None:
        return usage_error(
            f"Unknown flag: {flag}",
            f"Valid flags: {', '.join(_ROOT_FLAGS)}; commands: "
            f"{', '.join(_COMMANDS)}; usage: {PROG} <command> [options]",
        )
    return usage_error(
        f"Unknown flag for {name}: {flag}",
        _flag_help(name),
    )


def _repeated_flag_error(name: str, flag: str) -> CliError:
    flag = _printable(flag)
    return usage_error(
        f"Repeated flag for {name}: {flag}",
        f"Give {flag} at most once; {_flag_help(name)}",
    )


def _switch_value_error(name: str, flag: str) -> CliError:
    flag = _printable(flag)
    return usage_error(
        f"Flag {flag} for {name} does not take a value",
        _flag_help(name),
    )


def _missing_value_error(name: str, flag: str) -> CliError:
    flag = _printable(flag)
    return usage_error(
        f"Flag {flag} for {name} needs a value",
        f"Write {flag} followed by a value; {_flag_help(name)}",
    )


def _missing_argument_error(name: str) -> CliError:
    return usage_error(
        f"Missing required {_ROLE[name]} for {name}",
        f"Usage: {_USAGE[name]}; run {PROG} {name} --help for flags and examples",
    )


def _extra_argument_error(name: str) -> CliError:
    return usage_error(
        f"Unexpected extra argument: {name} takes {_POSITIONAL_SUMMARY[name]}",
        f"Usage: {_USAGE[name]}; run {PROG} {name} --help for flags and examples",
    )


def _unknown_command_error(token: str) -> CliError:
    return usage_error(
        f"Unknown command: {_printable(token)}",
        f"Supported commands: {', '.join(_COMMANDS)}. Run {PROG} --help for "
        f"usage; each command accepts --help.",
    )


def _parse_subcommand(name: str, tokens: Sequence[str]) -> ParsedCommand | None:
    """Classify the tokens after a command name in one left-to-right pass.

    Returns None when ``--help`` was classified as an option token: the caller
    then writes that command's help and ignores every recorded error.
    """
    value_flags = _VALUE_FLAGS[name]
    switches = _SWITCH_FLAGS[name]
    arity = _ARITY[name]
    options: dict[str, str] = {}
    seen_switches: set[str] = set()
    positionals: list[str] = []
    errors: list[CliError] = []
    help_requested = False
    delimiter = False
    pending: str | None = None

    for token in tokens:
        if pending is not None:
            options[pending] = token
            pending = None
            continue
        if delimiter:
            if len(positionals) < arity:
                positionals.append(token)
            else:
                errors.append(_extra_argument_error(name))
            continue
        if token == "--":
            delimiter = True
            continue
        if _is_option_token(token):
            flag = _option_name(token)
            if flag == "--help":
                help_requested = True
                continue
            if flag in value_flags:
                if flag in options:
                    errors.append(_repeated_flag_error(name, flag))
                elif "=" in token:
                    options[flag] = token.split("=", 1)[1]
                else:
                    pending = flag
                continue
            if flag in switches:
                if "=" in token:
                    errors.append(_switch_value_error(name, flag))
                elif flag in seen_switches:
                    errors.append(_repeated_flag_error(name, flag))
                else:
                    seen_switches.add(flag)
                continue
            errors.append(_unknown_flag_error(name, flag))
            continue
        if len(positionals) < arity:
            positionals.append(token)
        else:
            errors.append(_extra_argument_error(name))

    if pending is not None:
        errors.append(_missing_value_error(name, pending))
    if help_requested:
        return None
    if errors:
        raise errors[0]
    if arity and not positionals:
        raise _missing_argument_error(name)
    return ParsedCommand(
        name=name,
        argument=positionals[0] if arity else None,
        options=dict(options),
        switches=frozenset(seen_switches),
    )


# ---------------------------------------------------------------------------
# Documents


def _executable_path() -> str:
    path = os.path.abspath(sys.argv[0])
    home = os.path.expanduser("~").rstrip(os.sep)
    if home:
        if path == home:
            return "~"
        if path.startswith(home + os.sep):
            return "~" + path[len(home) :]
    return path


def _root_help_document() -> dict[str, object]:
    return {
        "usage": f"{PROG} <command> [options]",
        "commands": [
            (
                f"search <query> [--limit N] [--need-content] "
                f"[--fields FIELD,...] -- {_COMMAND_INFO['search'].summary}"
            ),
            f"fetch <url> [--full] -- {_COMMAND_INFO['fetch'].summary}",
            f"doctor -- {_COMMAND_INFO['doctor'].summary}",
        ],
        "discovery": [
            f"{PROG} --help -- show this help",
            f"{PROG} --version, -v, -V -- print the installed version",
            (
                f"{PROG} <command> --help -- show a command's arguments, "
                "flags, defaults and examples"
            ),
        ],
        "environment": [
            (
                f"{API_KEY_ENV} -- required for search and fetch; read from "
                "the environment, ./.env, or ~/.config/deepsearch-axi/.env"
            ),
            (
                f"{BACKEND_ENV} -- test-only backend origin override; unset in "
                "normal use. Accepts an https origin, or an http origin whose "
                "host is a literal loopback IP."
            ),
        ],
        "help": f"Run {PROG} <command> --help for flags, defaults and examples.",
    }


def _search_help_document() -> dict[str, object]:
    return {
        "command": f"search -- {_COMMAND_INFO['search'].summary}",
        "usage": _USAGE["search"],
        "argument": _COMMAND_INFO["search"].argument,
        "flags": [
            (
                f"--limit N: maximum number of results, "
                f"{SEARCH_LIMIT_MIN}-{SEARCH_LIMIT_MAX} inclusive; default "
                f"{SEARCH_LIMIT_DEFAULT}; --limit=N is also accepted"
            ),
            (
                "--need-content: request page content and add content to the "
                "default title,url,snippet projection"
            ),
            (
                "--fields FIELD,...: ordered projection from title,url,snippet,"
                "summary,content,date,authority,rerank_score,site_name,site_icon; "
                "replaces the default projection and wins over --need-content"
            ),
            "--help: show this help",
        ],
        "notes": (
            "Put -- before a query that starts with - so it stays the query. "
            "Fields are projected locally after the service response; only "
            "--need-content changes the request."
        ),
        "examples": [
            f'{PROG} search "rust async" --limit 5',
            (
                f'{PROG} search "python packaging" --need-content '
                "--fields title,url,snippet,date,content"
            ),
            f'{PROG} search "python packaging" --fields title,url,date',
            f"{PROG} search --limit 1 -- --help",
        ],
    }


def _fetch_help_document() -> dict[str, object]:
    return {
        "command": f"fetch -- {_COMMAND_INFO['fetch'].summary}",
        "usage": _USAGE["fetch"],
        "argument": _COMMAND_INFO["fetch"].argument,
        "flags": [
            (
                f"--full: return the complete page text instead of the "
                f"default {FETCH_PREVIEW_CHARS}-character preview"
            ),
            "--help: show this help",
        ],
        "notes": (
            f"The default view keeps the first {FETCH_PREVIEW_CHARS} "
            "characters; --full returns all of it. A validated URL never "
            "starts with -, so -- is optional."
        ),
        "examples": [
            f'{PROG} fetch "https://docs.python.org/3/library/argparse.html"',
            f'{PROG} fetch "https://docs.python.org/3/library/argparse.html" --full',
        ],
    }


def _doctor_help_document() -> dict[str, object]:
    return {
        "command": f"doctor -- {_COMMAND_INFO['doctor'].summary}",
        "usage": _USAGE["doctor"],
        "description": (
            "Report local credential presence and the effective backend "
            "origin. It does not verify remote authentication and makes no "
            "network request."
        ),
        "environment": [
            (
                f"{API_KEY_ENV}: required for search and fetch; read from the "
                "environment, ./.env, or ~/.config/deepsearch-axi/.env"
            ),
            (
                f"{BACKEND_ENV}: test-only backend origin override; unset in "
                "normal use. Accepts an https origin, or an http origin whose "
                "host is a literal loopback IP."
            ),
        ],
        "examples": [
            f"{PROG} doctor",
            f"env -u {BACKEND_ENV} {PROG} doctor",
        ],
    }


def _command_help_document(name: str) -> dict[str, object]:
    if name == "search":
        return _search_help_document()
    if name == "fetch":
        return _fetch_help_document()
    return _doctor_help_document()


def _credential_source_label(source: str) -> str:
    return {
        "env": "the environment",
        "cwd_dotenv": "./.env",
        "user_dotenv": "~/.config/deepsearch-axi/.env",
    }.get(source, "an unknown source")


def _home_document() -> dict[str, object]:
    document: dict[str, object] = {
        "bin": _executable_path(),
        "description": "Search the web and fetch page text through DeepSeek's "
        "native Search and Fetch API.",
    }
    status = config.credential_status(os.environ, Path.cwd(), Path.home())
    if status.state == "configured":
        document["credential"] = (
            f"{API_KEY_ENV} is available locally (source: "
            f"{_credential_source_label(status.source)})."
        )
    elif status.state == "invalid":
        document["credential"] = (
            f"{API_KEY_ENV} was found but is not usable; run {PROG} doctor for "
            "the corrective error."
        )
    else:
        document["credential"] = (
            f"No DeepSeek API key found in {API_KEY_ENV}, ./.env, or "
            f"~/.config/deepsearch-axi/.env; export {API_KEY_ENV}=... before "
            "search or fetch."
        )
    try:
        backend = config.resolve_backend(os.environ)
    except CliError:
        document["backend"] = (
            f"Effective backend unavailable: {BACKEND_ENV} is set but is not a "
            f"valid origin; run {PROG} doctor for the corrective error, or "
            "unset it."
        )
    else:
        if backend.override:
            document["backend"] = (
                f"Effective backend: {backend.origin} (test override active via {BACKEND_ENV})."
            )
        else:
            document["backend"] = (
                f"Effective backend: {backend.origin} (default; {BACKEND_ENV} is not set)."
            )
    document["help"] = [
        f'{PROG} search "<query>" [--limit N] -- search the web',
        f"{PROG} fetch <url> [--full] -- fetch page text",
        f"{PROG} doctor -- check local credential presence and the effective backend",
        f"{PROG} <command> --help -- flags, defaults and examples",
    ]
    return document


def _doctor_document() -> dict[str, object]:
    settings = config.load_settings(os.environ, Path.cwd(), Path.home())
    backend = settings.backend
    if backend.override:
        override = f"{BACKEND_ENV} is set: the test-only backend override is active."
    else:
        override = f"{BACKEND_ENV} is not set: the default backend origin is used."
    return {
        "credential": f"{API_KEY_ENV} is present locally.",
        "authentication": "Local check only: remote authentication was not "
        "verified and no request was made.",
        "backend": f"Effective backend origin: {backend.origin}",
        "override": override,
    }


# ---------------------------------------------------------------------------
# Execution


def _internal_error() -> CliError:
    return CliError(
        ExitCode.FAILURE,
        "An unexpected internal error occurred.",
        "Rerun the command; report the problem if it persists.",
    )


def _execute(args: Sequence[str], secrets: tuple[str, ...] = ()) -> None:
    if not args:
        output.write_document(_home_document(), secrets=secrets)
        return

    first, rest = args[0], list(args[1:])
    if first in _VERSION_FLAGS:
        if rest:
            raise usage_error(
                f"{first} takes no further arguments.",
                f"Run {PROG} {first} on its own, or run {PROG} --help.",
            )
        output.write_version(importlib.metadata.version(DIST_NAME), secrets=secrets)
        return
    if first == "--help":
        if rest:
            raise usage_error(
                "--help takes no further arguments.",
                f"Run {PROG} --help on its own; each command accepts its own --help.",
            )
        output.write_document(_root_help_document(), secrets=secrets)
        return
    if _is_option_token(first):
        raise _unknown_flag_error(None, _option_name(first))
    if first not in _COMMANDS:
        raise _unknown_command_error(first)

    parsed = _parse_subcommand(first, rest)
    if parsed is None:
        output.write_document(_command_help_document(first), secrets=secrets)
        return
    if first == "doctor":
        output.write_document(_doctor_document(), secrets=secrets)
        return

    module = importlib.import_module(f"deepsearch_axi.{first}")
    request = module.validate(parsed)
    settings = config.load_settings(os.environ, Path.cwd(), Path.home())
    post = functools.partial(transport.post_json, settings)
    output.write_document(module.execute(request, post), secrets=secrets)


def _write_error_document(error: CliError, secrets: tuple[str, ...]) -> int:
    try:
        output.write_document(output.error_document(error), secrets=secrets)
    except Exception:
        pass
    return int(error.exit_code)


def _redaction_secrets() -> tuple[str, ...]:
    """Resolve the effective key for the output boundary, never for display."""
    try:
        value = config.effective_secret(os.environ, Path.cwd(), Path.home())
    except Exception:
        return ()
    return (value,) if value else ()


def main(argv: Sequence[str] | None = None) -> int:
    warnings.simplefilter("ignore")
    args = list(sys.argv[1:] if argv is None else argv)
    secrets = _redaction_secrets()
    try:
        _execute(args, secrets)
        return int(ExitCode.OK)
    except CliError as error:
        return _write_error_document(error, secrets)
    except Exception:
        return _write_error_document(_internal_error(), secrets)
