"""Static guidance for deepsearch-axi: schema, help text, hints and skill text.

This module imports only ``dataclasses`` and ``typing`` (plus ``__future__``)
so that ``scripts/generate_skill.py`` can load it by file path without
installing the package. It is the single source for the parser schema,
home/help hints, and the generated skill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class ArgSpec:
    name: str
    description: str


@dataclass(frozen=True)
class FlagSpec:
    name: str  # "--count"
    metavar: Optional[str]  # "N"; None => boolean switch
    default: str  # display default: "10", "false", '""', "omitted", "all", "title,url,snippet"
    description: str
    choices: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandSpec:
    path: Tuple[str, ...]
    summary: str
    usage: str
    arguments: Tuple[ArgSpec, ...]
    flags: Tuple[FlagSpec, ...]  # --help is implicit, appended by documents.help_document
    examples: Tuple[str, ...]  # 2-3 items
    subcommands: Tuple[str, ...] = ()


DESCRIPTION = "Agent-native CLI for DeepSeek web search and page fetch with compact TOON output."
PROJECT_INSTRUCTION = (
    "Use deepsearch-axi for DeepSeek web search and page fetch; run deepsearch-axi "
    "for local readiness and follow its contextual TOON hints."
)
REPO_URL = "https://github.com/ddbaron/deepsearch-axi"

HINT_SEARCH = 'deepsearch-axi search "<query>"'
HINT_FETCH = "deepsearch-axi fetch <url>"
HINT_FETCH_FULL = "deepsearch-axi fetch <url> --full"
HINT_SET_KEY = "export DEEPSEEK_API_KEY=<your-deepseek-api-key>"
HINT_SETUP = "deepsearch-axi setup"

_FIELD_CHOICES: Tuple[str, ...] = (
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
_AGENT_CHOICES: Tuple[str, ...] = ("claude-code", "codex", "opencode")
_AGENT_SELECTION_CHOICES: Tuple[str, ...] = ("claude-code", "codex", "opencode", "all")

_COMMANDS: Tuple[CommandSpec, ...] = (
    CommandSpec(
        path=(),
        summary="Agent-native DeepSeek web search and page fetch with compact TOON output.",
        usage="deepsearch-axi [command] [flags]",
        arguments=(),
        flags=(
            FlagSpec(
                "-v, -V, --version",
                None,
                "n/a",
                "print the installed version and exit (sole argument only)",
            ),
        ),
        examples=(
            "deepsearch-axi",
            'deepsearch-axi search "site:docs.python.org asyncio TaskGroup"',
            "deepsearch-axi fetch https://docs.python.org/3/library/asyncio-task.html",
        ),
        subcommands=("search", "fetch", "setup", "session"),
    ),
    CommandSpec(
        path=("search",),
        summary="Search the web through DeepSeek and print compact TOON results.",
        usage="deepsearch-axi search <query> [flags]",
        arguments=(ArgSpec("query", "1-128 characters, passed unchanged including operators"),),
        flags=(
            FlagSpec("--count", "N", "10", "number of results to return (1-50)"),
            FlagSpec("--need-content", None, "false", "request page content for each result"),
            FlagSpec(
                "--ori-question",
                "TEXT",
                "omitted",
                "original question used for reranking (1-128 characters)",
            ),
            FlagSpec("--localization", "IP", "", "IPv4 or IPv6 localization hint"),
            FlagSpec(
                "--fields",
                "FIELD,...",
                "title,url,snippet",
                "comma-separated ordered result fields",
                _FIELD_CHOICES,
            ),
            FlagSpec("--full", None, "false", "remove text preview limits for the selected fields"),
        ),
        examples=(
            'deepsearch-axi search "rust async runtime after:2025-01-01"',
            "deepsearch-axi search '\"structured concurrency\" -site:reddit.com' "
            "--count 5 --fields title,url,summary,date",
            'deepsearch-axi search -- "-site:example.com tokio"',
        ),
    ),
    CommandSpec(
        path=("fetch",),
        summary="Fetch a page as Markdown through DeepSeek and print compact TOON.",
        usage="deepsearch-axi fetch <url> [flags]",
        arguments=(ArgSpec("url", "http(s) URL up to 8192 characters"),),
        flags=(
            FlagSpec("--full", None, "false", "remove text preview limits for the page fields"),
        ),
        examples=(
            "deepsearch-axi fetch https://example.com/article",
            "deepsearch-axi fetch https://example.com/article --full",
        ),
    ),
    CommandSpec(
        path=("setup",),
        summary=(
            "Install or repair Claude Code, Codex, and OpenCode session integrations "
            "with one shared project record; a moved project directory is refused "
            "until that record is removed. After moving the executable, run full setup; "
            "--agent repairs only its selected host and can leave other hosts stale."
        ),
        usage="deepsearch-axi setup [flags]",
        arguments=(),
        flags=(
            FlagSpec(
                "--agent",
                "NAME",
                "all",
                "integration to configure (one host or all)",
                _AGENT_SELECTION_CHOICES,
            ),
        ),
        examples=(
            "deepsearch-axi setup",
            "deepsearch-axi setup --agent codex",
        ),
    ),
    CommandSpec(
        path=("session",),
        summary=(
            "Record a native session lifecycle event from host hook JSON on stdin; "
            "the native payload cwd selects the ambient-context and state scope."
        ),
        usage="deepsearch-axi session <start|end> --agent NAME --project ABS_DIR",
        arguments=(),
        flags=(),
        examples=(
            'deepsearch-axi session start --agent codex --project "$PWD" < event.json',
            'deepsearch-axi session end --agent codex --project "$PWD" < event.json',
        ),
        subcommands=("start", "end"),
    ),
    CommandSpec(
        path=("session", "start"),
        summary=(
            "Record a session start and print compact ambient context from native hook JSON "
            "on stdin (max 64 KiB); the native payload cwd selects the ambient-context "
            "and state scope."
        ),
        usage="deepsearch-axi session start --agent NAME --project ABS_DIR",
        arguments=(),
        flags=(
            FlagSpec(
                "--agent",
                "NAME",
                "required",
                "host integration reporting the event",
                _AGENT_CHOICES,
            ),
            FlagSpec(
                "--project",
                "ABS_DIR",
                "required",
                "absolute project directory that must contain the payload cwd",
            ),
        ),
        examples=(
            'deepsearch-axi session start --agent claude-code --project "$PWD" < event.json',
            'deepsearch-axi session start --agent opencode --project "$PWD" < event.json',
        ),
    ),
    CommandSpec(
        path=("session", "end"),
        summary=(
            "Record a session checkpoint or end from native hook JSON on stdin "
            "(max 64 KiB); session.idle and plugin.dispose payloads record a checkpoint."
        ),
        usage="deepsearch-axi session end --agent NAME --project ABS_DIR",
        arguments=(),
        flags=(
            FlagSpec(
                "--agent",
                "NAME",
                "required",
                "host integration reporting the event",
                _AGENT_CHOICES,
            ),
            FlagSpec(
                "--project",
                "ABS_DIR",
                "required",
                "absolute project directory that must contain the payload cwd",
            ),
        ),
        examples=(
            'deepsearch-axi session end --agent claude-code --project "$PWD" < event.json',
            'deepsearch-axi session end --agent opencode --project "$PWD" < event.json',
        ),
    ),
)

COMMANDS: Dict[Tuple[str, ...], CommandSpec] = {spec.path: spec for spec in _COMMANDS}

_SUPPRESSED_DEFAULT_LABELS = frozenset({"false", "omitted", "required", "n/a"})


def flags_line(path: Tuple[str, ...]) -> str:
    """Render the one-line flag inventory used by usage failures and home hints."""
    if path == ():
        return "commands: search, fetch, setup, session; flags: --help, -v|-V|--version (alone)"
    spec = COMMANDS.get(path)
    if spec is None:
        return "--help"
    rendered = []
    for flag in spec.flags:
        text = flag.name if flag.metavar is None else f"{flag.name} {flag.metavar}"
        if flag.default and flag.default not in _SUPPRESSED_DEFAULT_LABELS:
            text += f" (default {flag.default})"
        rendered.append(text)
    rendered.append("--help")
    return ", ".join(rendered)


def _flag_label(flag: FlagSpec) -> str:
    return flag.name if flag.metavar is None else f"{flag.name} {flag.metavar}"


def _flag_table(spec: CommandSpec) -> str:
    lines = ["| Flag | Default | Description |", "| --- | --- | --- |"]
    for flag in spec.flags:
        default = flag.default if flag.default else "(empty string)"
        lines.append(f"| `{_flag_label(flag)}` | `{default}` | {flag.description} |")
    lines.append("| `--help` | `false` | show help and exit |")
    return "\n".join(lines)


def _command_section(spec: CommandSpec) -> str:
    title = " ".join(("deepsearch-axi",) + spec.path)
    lines = [f"### {title}", "", spec.summary, "", "```text", spec.usage, "```", ""]
    if spec.arguments:
        for argument in spec.arguments:
            lines.append(f"- `<{argument.name}>`: {argument.description}")
        lines.append("")
    if spec.subcommands:
        lines.append("Subcommands: " + ", ".join(f"`{name}`" for name in spec.subcommands) + ".")
        lines.append("")
    lines.append(_flag_table(spec))
    lines.append("")
    lines.append("Examples:")
    lines.append("")
    for example in spec.examples:
        lines.append(f"- `{example}`")
    lines.append("")
    return "\n".join(lines)


def skill_markdown() -> str:
    """Return the deterministic, committed agent skill document (LF, one trailing newline)."""
    intro_description = (
        "Search the web and fetch pages as Markdown through DeepSeek with compact "
        "TOON output. Use when a task needs current web results, source URLs, or "
        "the contents of a specific page."
    )
    sections = [
        "---",
        "name: deepsearch-axi",
        f'description: "{intro_description}"',
        "---",
        "",
        "# deepsearch-axi",
        "",
        DESCRIPTION,
        "",
        "## When to use",
        "",
        "- A task needs current web results, source URLs, or the contents of a specific page.",
        "- Local readiness is useful before research: run `deepsearch-axi` with no arguments.",
        "- Skip it when the repository already answers the question or no web evidence is needed.",
        "",
        "## Setup-free invocation",
        "",
        "Run from a Git checkout without installing:",
        "",
        "```sh",
        f'uvx --from git+{REPO_URL}.git deepsearch-axi search "<query>"',
        "```",
        "",
        "Set `DEEPSEEK_API_KEY` in the environment, in `./.env`, or in "
        "`~/.config/deepsearch-axi/.env` before searching or fetching.",
        "",
        "## Commands",
        "",
    ]
    sections.append(_command_section(COMMANDS[("search",)]))
    sections.append(_command_section(COMMANDS[("fetch",)]))
    sections.append(_command_section(COMMANDS[("setup",)]))
    sections.append(_command_section(COMMANDS[("session",)]))
    sections.append(_command_section(COMMANDS[("session", "start")]))
    sections.append(_command_section(COMMANDS[("session", "end")]))
    sections.extend(
        [
            "## Output, truncation, and empty results",
            "",
            "Every document is TOON with 2-space indentation and LF line endings. "
            "Search rows contain only the selected fields, in order. Defaults are "
            "`title,url,snippet`; `--need-content` adds `content`; `--fields` replaces "
            "the projection with an ordered allowlist.",
            "",
            "Text previews truncate to 200 code points for title and snippet and 1000 "
            "for summary and content. A `truncated` array reports `path`, `shown`, and "
            "`total` only when a value was cut. `--full` removes those preview limits "
            "but not the 8 MiB response cap, count limit, or schema validation.",
            "",
            "An empty successful search reports `count: 0`, `total: 0`, "
            '`total_scope: response`, and `message: "No results returned for this query."` '
            "with a query-refinement hint; it is not an error.",
            "",
            "## Errors and exit codes",
            "",
            "Errors are TOON documents with an `error.code`, a safe `message`, and one "
            "or two actionable hints. Exit codes: `0` success, `1` execution or "
            "configuration failure (including all HTTP statuses), `2` local usage "
            "failure. No command prompts, and no raw response body, header, URL "
            "credential, or secret is printed.",
            "",
            "## Project instruction",
            "",
            PROJECT_INSTRUCTION,
            "",
        ]
    )
    return "\n".join(sections)
