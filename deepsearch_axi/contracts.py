"""Cross-module contracts for deepsearch-axi. Standard library only."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum

PROG = "deepsearch-axi"
DIST_NAME = "deepsearch-axi"

API_KEY_ENV = "DEEPSEEK_API_KEY"
BACKEND_ENV = "DEEPSEARCH_AXI_BACKEND_URL"
DEFAULT_BACKEND_ORIGIN = "https://api.deepseek.com"

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

OPERATION_TIMEOUT_SECONDS = 30.0

QUERY_MAX_CHARS = 128
SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 50
URL_MAX_CHARS = 8192
FETCH_PREVIEW_CHARS = 1000


class ExitCode(IntEnum):
    OK = 0
    FAILURE = 1
    USAGE = 2


class CliError(Exception):
    """A translated, user-facing failure. Never carries raw dependency text."""

    def __init__(self, exit_code: ExitCode, message: str, help_line: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.help_line = help_line


def usage_error(message: str, help_line: str) -> CliError:
    return CliError(ExitCode.USAGE, message, help_line)


def failure(message: str, help_line: str) -> CliError:
    return CliError(ExitCode.FAILURE, message, help_line)


def unusable_response_error() -> CliError:
    return CliError(
        ExitCode.FAILURE,
        "The service response was unusable: malformed, incomplete or unexpected data.",
        "Rerun the command; report the problem if it persists.",
    )


@dataclass(frozen=True)
class ParsedCommand:
    """A grammatically valid subcommand invocation (values not yet validated).

    name:     "search" | "fetch" | "doctor"
    argument: the single positional exactly as received from argv
              (may be "" or whitespace); None only for commands without one
    options:  value flags actually given, keyed with dashes, e.g. {"--limit": "5"}
    switches: boolean flags actually given, e.g. frozenset({"--full"})
    """

    name: str
    argument: str | None
    options: Mapping[str, str]
    switches: frozenset[str]


@dataclass(frozen=True)
class BackendOrigin:
    """Validated effective backend origin.

    origin: scheme://netloc with no trailing slash, e.g. "https://api.deepseek.com",
            "http://127.0.0.1:5555", "http://[::1]:9"
    scheme: "https" | "http"
    host:   hostname without IPv6 brackets, e.g. "api.deepseek.com", "::1"
    port:   explicit port or None
    override: True when DEEPSEARCH_AXI_BACKEND_URL is set (validly)
    """

    origin: str
    scheme: str
    host: str
    port: int | None
    override: bool


@dataclass(frozen=True)
class Settings:
    backend: BackendOrigin
    api_key: str = field(repr=False)


# Bound transport seam: post(path, payload) -> parsed JSON value.
# Performs exactly one request; raises CliError on every failure.
PostJson = Callable[[str, Mapping[str, object]], object]
