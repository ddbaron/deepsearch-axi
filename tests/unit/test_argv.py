"""argv parser tests: grammar, help handling, safe usage failures, no I/O."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

from deepsearch_axi import models
from deepsearch_axi.argv import parse_args
from deepsearch_axi.guidance import COMMANDS
from deepsearch_axi.models import (
    DeepsearchError,
    FetchCommand,
    HelpCommand,
    HomeCommand,
    OutputOptions,
    SearchCommand,
    SearchOptions,
    SessionCommand,
    SetupCommand,
)


def _usage_failure(argv: list[str]) -> models.Failure:
    with pytest.raises(DeepsearchError) as info:
        parse_args(argv)
    failure = info.value.failure
    assert failure.code == "usage"
    assert failure.http_status is None
    assert models.exit_code_for(failure) == models.EXIT_USAGE
    assert len(failure.help) == 2
    assert failure.help[0].startswith("valid flags for ")
    return failure


def test_empty_argv_is_home() -> None:
    assert parse_args([]) == HomeCommand()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(["--help"], HelpCommand(()), id="root"),
        pytest.param(["search", "--help"], HelpCommand(("search",)), id="search"),
        pytest.param(["fetch", "--help"], HelpCommand(("fetch",)), id="fetch"),
        pytest.param(["setup", "--help"], HelpCommand(("setup",)), id="setup"),
        pytest.param(["session", "--help"], HelpCommand(("session",)), id="session"),
        pytest.param(["session", "start", "--help"], HelpCommand(("session", "start")), id="start"),
        pytest.param(["session", "end", "--help"], HelpCommand(("session", "end")), id="end"),
    ],
)
def test_help_commands(argv: list[str], expected: HelpCommand) -> None:
    assert parse_args(argv) == expected


def test_search_defaults() -> None:
    command = parse_args(["search", "site:python.org -asyncio"])
    assert command == SearchCommand(
        options=SearchOptions(query="site:python.org -asyncio"),
        output=OutputOptions(fields=("title", "url", "snippet")),
    )


def test_search_flag_defaults_and_full_option() -> None:
    command = parse_args(["search", "q", "--full"])
    assert command == SearchCommand(
        options=SearchOptions(query="q"),
        output=OutputOptions(fields=("title", "url", "snippet"), full=True),
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["--count", "1"],
            SearchOptions(query="q", count=1),
            id="count-separate-min",
        ),
        pytest.param(
            ["--count=50"],
            SearchOptions(query="q", count=50),
            id="count-inline-max",
        ),
        pytest.param(
            ["--need-content"],
            SearchOptions(query="q", need_content=True),
            id="need-content",
        ),
        pytest.param(
            ["--ori-question", "original"],
            SearchOptions(query="q", ori_question="original"),
            id="ori-question",
        ),
        pytest.param(
            ["--ori-question= "],
            SearchOptions(query="q", ori_question=" "),
            id="ori-question-whitespace",
        ),
        pytest.param(
            ["--localization", "127.0.0.1"],
            SearchOptions(query="q", localization="127.0.0.1"),
            id="localization-ipv4",
        ),
        pytest.param(
            ["--localization=::1"],
            SearchOptions(query="q", localization="::1"),
            id="localization-ipv6",
        ),
        pytest.param(
            ["--localization", ""],
            SearchOptions(query="q", localization=""),
            id="localization-empty",
        ),
        pytest.param(
            [
                "--count",
                "5",
                "--need-content",
                "--ori-question",
                "why",
                "--localization",
                "2001:db8::1",
                "--full",
            ],
            SearchOptions(
                query="q",
                count=5,
                need_content=True,
                ori_question="why",
                localization="2001:db8::1",
            ),
            id="all-options",
        ),
    ],
)
def test_search_options(argv: list[str], expected: SearchOptions) -> None:
    command = parse_args(["search", *argv, "q"])
    assert isinstance(command, SearchCommand)
    assert command.options == expected


@pytest.mark.parametrize(
    ("argv", "expected_fields"),
    [
        pytest.param(["--fields", "title"], ("title",), id="single"),
        pytest.param(["--fields=title,url,date"], ("title", "url", "date"), id="ordered"),
        pytest.param(["--fields", " title , url ,date "], ("title", "url", "date"), id="trimmed"),
        pytest.param(
            ["--need-content"],
            ("title", "url", "snippet", "content"),
            id="need-content-adds-content",
        ),
        pytest.param(
            ["--need-content", "--fields", "title,summary"],
            ("title", "summary"),
            id="explicit-fields-win",
        ),
    ],
)
def test_search_projection(argv: list[str], expected_fields: tuple[str, ...]) -> None:
    command = parse_args(["search", *argv, "q"])
    assert isinstance(command, SearchCommand)
    assert command.output.fields == expected_fields


_QUERY_CASES = [
    pytest.param("a", id="one-char"),
    pytest.param("a" * 128, id="128-chars"),
    pytest.param(" e\u0301 ", id="nfd-spaces"),
    pytest.param("\u00e9", id="nfc"),
    pytest.param("e\u0301", id="nfd"),
    pytest.param("\U0001f40d", id="emoji"),
    pytest.param("a  b", id="double-space"),
    pytest.param('"structured concurrency" -site:reddit.com', id="operators"),
    pytest.param("after:2025-01-01 OR lang:en", id="server-syntax"),
]


@pytest.mark.parametrize("query", _QUERY_CASES)
def test_search_preserves_query_exactly(query: str) -> None:
    command = parse_args(["search", query])
    assert isinstance(command, SearchCommand)
    assert command.options.query == query


def test_double_dash_makes_hyphen_leading_query_positional() -> None:
    command = parse_args(["search", "--", "-site:example.com tokio"])
    assert isinstance(command, SearchCommand)
    assert command.options.query == "-site:example.com tokio"
    assert command.options.count == 10


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["https://example.com/article"],
            FetchCommand(url="https://example.com/article"),
            id="plain",
        ),
        pytest.param(
            ["https://example.com/a?b=1#c", "--full"],
            FetchCommand(url="https://example.com/a?b=1#c", full=True),
            id="query-fragment-full",
        ),
        pytest.param(
            ["--full", "http://example.com"],
            FetchCommand(url="http://example.com", full=True),
            id="full-before",
        ),
        pytest.param(
            ["--", "https://example.com/x"],
            FetchCommand(url="https://example.com/x"),
            id="delimiter",
        ),
    ],
)
def test_fetch_commands(argv: list[str], expected: FetchCommand) -> None:
    assert parse_args(["fetch", *argv]) == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param([], "all", id="default"),
        pytest.param(["--agent", "claude-code"], "claude-code", id="claude"),
        pytest.param(["--agent=codex"], "codex", id="codex-inline"),
        pytest.param(["--agent", "opencode"], "opencode", id="opencode"),
        pytest.param(["--agent", "all"], "all", id="explicit-all"),
    ],
)
def test_setup_commands(argv: list[str], expected: str) -> None:
    assert parse_args(["setup", *argv]) == SetupCommand(agent=expected)


@pytest.mark.parametrize(
    ("argv", "event", "agent"),
    [
        pytest.param(
            ["start", "--agent", "codex", "--project", "/tmp/p"],
            "start",
            "codex",
            id="start",
        ),
        pytest.param(
            ["end", "--agent=claude-code", "--project=/tmp/p"],
            "end",
            "claude-code",
            id="end-inline",
        ),
        pytest.param(
            ["start", "--project", "/tmp/p", "--agent", "opencode"],
            "start",
            "opencode",
            id="reordered",
        ),
    ],
)
def test_session_commands(argv: list[str], event: str, agent: str) -> None:
    assert parse_args(["session", *argv]) == SessionCommand(
        event=event,
        agent=agent,
        project=Path("/tmp/p"),
    )


def test_help_tolerates_missing_positionals_and_unvalidated_values() -> None:
    assert parse_args(["search", "--help"]) == HelpCommand(("search",))
    assert parse_args(["fetch", "--help"]) == HelpCommand(("fetch",))
    assert parse_args(["session", "start", "--help"]) == HelpCommand(("session", "start"))
    assert parse_args(["search", "--help", "--count", "not-a-number"]) == HelpCommand(("search",))
    assert parse_args(["setup", "--help", "--agent", "bogus"]) == HelpCommand(("setup",))
    assert parse_args(["search", "q", "--help"]) == HelpCommand(("search",))


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(["nope"], "unknown command or flag an unrecognized argument", id="command"),
        pytest.param(["--json"], "unknown command or flag --json", id="root-json"),
        pytest.param(["--api-key"], "unknown command or flag --api-key", id="root-api-key"),
        pytest.param(["---weird"], "unknown command or flag an unrecognized argument", id="unsafe"),
        pytest.param(
            ["search", "q", "--bogus"],
            "unknown flag --bogus for search",
            id="unknown-flag",
        ),
        pytest.param(["search", "q", "--json"], "unknown flag --json for search", id="json"),
        pytest.param(
            ["search", "q", "--api-key", "x"],
            "unknown flag --api-key for search",
            id="api-key",
        ),
        pytest.param(["search", "q", "--coun", "5"], "unknown flag --coun for search", id="abbrev"),
        pytest.param(
            ["search", "q", "--count", "1", "--count", "2"],
            "repeated flag --count for search",
            id="repeated",
        ),
        pytest.param(
            ["search", "q", "--full", "--full"],
            "repeated flag --full for search",
            id="repeated-bool",
        ),
        pytest.param(["search"], "missing <query> for search", id="missing-query"),
        pytest.param(["fetch"], "missing <url> for fetch", id="missing-url"),
        pytest.param(["search", "a", "b"], "too many arguments for search", id="extra-search"),
        pytest.param(
            ["fetch", "https://example.com", "x"],
            "too many arguments for fetch",
            id="extra-fetch",
        ),
        pytest.param(["setup", "x"], "too many arguments for setup", id="extra-setup"),
        pytest.param(["search", "q", "--count"], "missing value for --count", id="missing-value"),
        pytest.param(
            ["search", "q", "--full=1"], "invalid value for --full", id="equals-on-switch"
        ),
        pytest.param(["search", "q", "--help=1"], "invalid value for --help", id="equals-on-help"),
        pytest.param(
            ["session"], "missing <start|end> for session", id="session-missing-subcommand"
        ),
        pytest.param(
            ["session", "bogus"],
            "unknown subcommand for session",
            id="session-subcommand",
        ),
        pytest.param(
            ["session", "--agent", "codex"],
            "unknown flag --agent for session",
            id="session-group-flag",
        ),
        pytest.param(
            ["session", "start"], "missing --agent for session start", id="session-missing-agent"
        ),
        pytest.param(
            ["session", "start", "--agent", "codex"],
            "missing --project for session start",
            id="session-missing-project",
        ),
        pytest.param(
            ["session", "start", "--agent", "bogus", "--project", "/tmp/p"],
            "invalid value for --agent",
            id="session-bad-agent",
        ),
        pytest.param(
            ["session", "start", "--agent", "codex", "--project", "relative"],
            "invalid value for --project",
            id="session-relative-project",
        ),
        pytest.param(
            ["search", "--help", "--bogus"],
            "unknown flag --bogus for search",
            id="help-unknown-flag",
        ),
        pytest.param(
            ["search", "a", "b", "--help"],
            "too many arguments for search",
            id="help-extra-positional",
        ),
    ],
)
def test_usage_messages(argv: list[str], message: str) -> None:
    failure = _usage_failure(argv)
    assert failure.message == message


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["-v"], id="short-v"),
        pytest.param(["-V"], id="short-V"),
        pytest.param(["--version"], id="long"),
        pytest.param(["--version", "x"], id="long-extra"),
        pytest.param(["-v", "search"], id="short-v-extra"),
        pytest.param(["search", "q", "-v"], id="trailing-short"),
        pytest.param(["search", "q", "--version"], id="trailing-long"),
        pytest.param(["fetch", "https://example.com", "-V"], id="trailing-fetch"),
        pytest.param(["setup", "--version"], id="trailing-setup"),
        pytest.param(["session", "start", "-v"], id="trailing-session"),
        pytest.param(["session", "-v"], id="session-alias"),
        pytest.param(["--version=1"], id="inline"),
    ],
)
def test_version_aliases_are_usage_errors(argv: list[str]) -> None:
    failure = _usage_failure(argv)
    assert failure.message == "--version is only valid alone: deepseek-axi --version"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["search", ""], id="empty-query"),
        pytest.param(["search", " "], id="blank-query"),
        pytest.param(["search", " " * 128], id="whitespace-query"),
        pytest.param(["search", "a" * 129], id="long-query"),
        pytest.param(["search", "\ud800"], id="surrogate-query"),
        pytest.param(["search", "q", "--count", "0"], id="count-zero"),
        pytest.param(["search", "q", "--count", "51"], id="count-high"),
        pytest.param(["search", "q", "--count", "abc"], id="count-text"),
        pytest.param(["search", "q", "--ori-question", ""], id="ori-empty"),
        pytest.param(["search", "q", "--ori-question", "x" * 129], id="ori-long"),
        pytest.param(["search", "q", "--localization", "localhost"], id="localization"),
        pytest.param(["search", "q", "--fields", ""], id="fields-empty"),
        pytest.param(["search", "q", "--fields", "title,title"], id="fields-duplicate"),
        pytest.param(["search", "q", "--fields", "title,unknown"], id="fields-unknown"),
        pytest.param(["fetch", "example.com"], id="fetch-no-scheme"),
        pytest.param(["fetch", "ftp://example.com"], id="fetch-scheme"),
        pytest.param(["fetch", "https://user@example.com"], id="fetch-userinfo"),
        pytest.param(["fetch", "https://example.com:abc"], id="fetch-port"),
        pytest.param(["fetch", "https://example.com\x00"], id="fetch-control"),
        pytest.param(["fetch", "https://example.com/" + "a" * 8192], id="fetch-long"),
    ],
)
def test_usage_failures_for_values(argv: list[str]) -> None:
    failure = _usage_failure(argv)
    assert failure.message.startswith("invalid value for ")
    assert failure.message != "--version is only valid alone: deepseek-axi --version"


def test_double_dash_trailing_version_alias_is_literal() -> None:
    command = parse_args(["search", "--", "-v"])
    assert isinstance(command, SearchCommand)
    assert command.options.query == "-v"


def test_double_dash_for_setup_and_extra_positionals() -> None:
    assert parse_args(["setup", "--"]) == SetupCommand(agent="all")
    with pytest.raises(DeepsearchError) as info:
        parse_args(["search", "--", "a", "b"])
    assert info.value.failure.message == "too many arguments for search"


def test_usage_help_mentions_command_flags_and_example() -> None:
    failure = _usage_failure(["search", "q", "--nope"])
    assert failure.help[0].startswith("valid flags for search: --count N")
    assert failure.help[1] == COMMANDS[("search",)].examples[0]

    root_failure = _usage_failure(["nope"])
    assert root_failure.help[0].startswith("valid flags for deepsearch-axi: commands: ")
    assert root_failure.help[1] == "deepsearch-axi"


def test_error_messages_never_echo_values_or_positionals() -> None:
    marker = "secret-marker-value"
    cases = [
        ["search", marker, marker],
        ["search", "q", "--count", marker],
        ["search", "q", "--fields", marker],
        ["search", "q", "--localization", marker],
        ["fetch", marker],
        ["session", "start", "--agent", marker, "--project", "/tmp/p"],
        ["session", "start", "--agent", "codex", "--project", marker],
    ]
    for argv in cases:
        failure = _usage_failure(argv)
        for text in (failure.message, *failure.help):
            assert marker not in text


def test_parser_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("parser must not perform I/O")

    class NoStdin:
        def read(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("parser must not read stdin")

        def readline(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("parser must not read stdin")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(builtins, "input", forbidden)
    monkeypatch.setattr(sys, "stdin", NoStdin())
    for argv in (
        [],
        ["--help"],
        ["search", "q"],
        ["search", "q", "--count", "10"],
        ["fetch", "https://example.com"],
        ["setup"],
        ["session", "start", "--agent", "codex", "--project", "/tmp/p"],
        ["search"],
        ["search", "q", "--nope"],
        ["nope"],
    ):
        try:
            parse_args(argv)
        except DeepsearchError:
            pass
