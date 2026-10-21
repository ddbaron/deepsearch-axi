"""AC-36: README, license, ignore rules and tracked-tree contracts."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
import toon_format

from deepsearch_axi import argv, documents, guidance, models
from deepsearch_axi.origin import validate_base_url

REPO_ROOT = Path(__file__).resolve().parents[1]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
AXI_PRINCIPLES = [
    "Token-efficient output",
    "Minimal default schemas",
    "Content truncation",
    "Precomputed aggregates",
    "Definitive empty states",
    "Structured errors and exit codes",
    "Ambient session context",
    "Content first",
    "Contextual disclosure",
    "Consistent help and identity",
]
CANONICAL_MIT = """
MIT License

Copyright (c) 2026 Derek Baron

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def _section(heading: str) -> str:
    start = README.index(heading) + len(heading)
    rest = README[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _code_block(section: str, language: str) -> str:
    blocks = re.findall(rf"```{language}\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 1, f"expected exactly one {language} block"
    return blocks[0].rstrip("\n")


def _worked_example() -> dict[str, object]:
    literal = _code_block(_section("## Worked example"), "text")
    decoded = toon_format.decode(literal, strict=True)
    assert isinstance(decoded, dict)
    return decoded


def _hint_command(hint: str, *, query: str, url: str) -> object:
    """Parse a contextual hint after substituting its documented placeholders."""
    tokens = shlex.split(hint)
    assert tokens[0] == "deepsearch-axi"
    substituted = [
        token.replace("<query>", query).replace("<url>", url).replace("<domain>", "docs.python.org")
        for token in tokens[1:]
    ]
    return argv.parse_args(substituted)


def _origin_values(cases: list[object]) -> set[object]:
    values: set[object] = set()
    for case in cases:
        values.add(case.values[0])  # type: ignore[attr-defined]
    return values


def _flag_rows() -> dict[tuple[str, str], str]:
    section = _section("## Flag reference")
    rows: dict[tuple[str, str], str] = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or cells[0] == "Command":
            continue
        if all(set(cell) <= {"-"} for cell in cells):
            continue
        rows[(cells[0].strip("`"), cells[1].strip("`"))] = cells[2].strip("`")
    return rows


def _expected_flags() -> dict[tuple[str, str], str]:
    expected: dict[tuple[str, str], str] = {}
    for path, spec in guidance.COMMANDS.items():
        command = " ".join(path) or "root"
        for flag in spec.flags:
            label = flag.name if flag.metavar is None else f"{flag.name} {flag.metavar}"
            expected[(command, label)] = flag.default or "(empty string)"
    return expected


def _ignored(path: str) -> bool:
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=30,
    )
    return completed.returncode == 0


def _tracked() -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]


def _forbidden(path: str) -> bool:
    parsed = Path(path)
    parts = set(parsed.parts)
    name = parsed.name
    directories = {
        ".deepsearch-axi",
        "build",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "wheelhouse",
        "node_modules",
        "secrets",
    }
    return (
        name in {"AGENTS.md", "CLAUDE.md", ".cursorrules", ".DS_Store"}
        or (name.startswith(".env") and name != ".env.example")
        or name.endswith((".pem", ".key", ".pyc"))
        or bool(parts & directories)
        or ".egg-info" in path
        or path.startswith((".agentfield-out-", ".worktrees/", ".artifacts/"))
    )


def test_readme_flag_reference_matches_parser_schema() -> None:
    assert _flag_rows() == _expected_flags()


def test_readme_install_examples_use_the_repository_url() -> None:
    for command in (
        "pipx install git+https://github.com/ddbaron/deepsearch-axi.git",
        "uv tool install git+https://github.com/ddbaron/deepsearch-axi.git",
        "python -m pip install git+https://github.com/ddbaron/deepsearch-axi.git",
    ):
        assert command in README
    assert "not published on PyPI" in README
    assert "pypi.org" not in README.lower()
    assert "pip install deepsearch-axi" not in README


def test_readme_documents_precedence_and_literal_dotenv_grammar() -> None:
    section = _section("## Configuration and security")
    positions = [
        section.index("DEEPSEEK_API_KEY"),
        section.index("`./.env`"),
        section.index("`~/.config/deepsearch-axi/.env`"),
    ]
    assert positions == sorted(positions)
    assert "Parent `.env` files are never searched" in section
    assert "optional `export` prefix" in section
    assert "first `=` separates the key" in section
    assert "single or double quotes" in section
    assert "no shell escapes" in section
    assert "environment mutations" in section


def test_readme_documents_origin_security_and_output_cost() -> None:
    section = _section("## Configuration and security")
    assert "DEEPSEEK_BASE_URL" in section
    assert "http://127.0.0.1:8080" in section
    assert "http://[::1]:8080" in section
    assert "trusted endpoint" in section
    assert "certificate verification" in section
    for token in ("redirect", "proxies", "netrc", "redacted", "30-second", "8 MiB"):
        assert token in section

    output = _section("## Output contract")
    assert "title 200, snippet 200, summary 1000, and content 1000" in output
    assert "4000 code points" in output
    assert "1000 code points per row" in output
    assert "1200" in output
    assert "characters/4" in output
    assert "total_scope" in output
    assert "truncated" in output
    assert "--full" in output
    assert "No results returned for this query." in output
    assert "Exit codes are 0 for success, 1 for execution" in output
    assert "2 for local usage" in output


def test_readme_documents_schemas_session_protocol_and_agent_setup() -> None:
    assert "page: {url, title, content}" in _section("## Output contract")
    assert "error: {code, message, http_status?}" in _section("## Output contract")
    assert "not a research command" in README
    assert ".claude/settings.json" in README
    assert ".codex/hooks.json" in README
    assert ".opencode/plugins/deepsearch-axi.js" in README
    assert ".deepsearch-axi/setup.lock" in README
    assert "1.18.29" not in README
    assert "Re-running setup after the executable moves repairs" in README
    assert "Moving the project directory after setup is not supported" in README

    integration = _section("## Agent integration")
    assert "Hooks are the primary integration" in integration
    assert "npx -y skills add ddbaron/deepsearch-axi --skill deepsearch-axi" in integration
    assert integration.index("deepsearch-axi setup") < integration.index("npx -y skills add")
    assert "python scripts/generate_skill.py --check" in integration


def test_readme_worked_example_is_strict_toon_with_projected_result() -> None:
    decoded = _worked_example()
    query = 'site:docs.python.org "TaskGroup" after:2024-01-01'

    assert decoded["query"] == query
    assert decoded["count"] == 1
    assert isinstance(decoded["count"], int) and not isinstance(decoded["count"], bool)
    assert decoded["total"] == 1
    assert decoded["total_scope"] == "response"

    results = decoded["results"]
    assert isinstance(results, list) and len(results) == 1
    row = results[0]
    assert list(row) == ["title", "url", "summary", "date", "authority"]
    assert row["title"] == "asyncio TaskGroup"
    assert row["url"] == "https://docs.python.org/3/library/asyncio-task.html"
    assert isinstance(row["summary"], str)
    assert row["date"] is None
    assert row["authority"] == 4
    assert isinstance(row["authority"], int) and not isinstance(row["authority"], bool)
    assert decoded["count"] == decoded["total"] == len(results)

    expected = documents.search_document(
        query,
        [dict(row)],
        models.OutputOptions(fields=tuple(row)),
        request=models.SearchOptions(query=query, count=5),
    )
    assert decoded == expected
    literal = _code_block(_section("## Worked example"), "text")
    assert toon_format.encode(expected, indent_size=2, delimiter=",") == literal


def test_readme_worked_example_hints_follow_the_supported_grammar() -> None:
    decoded = _worked_example()
    rows = decoded["results"]
    assert isinstance(rows, list) and len(rows) == 1
    url = rows[0]["url"]
    query = 'site:docs.python.org "TaskGroup" after:2024-01-01'

    hints = decoded["help"]
    assert isinstance(hints, list) and len(hints) == 2
    fetch, search = hints
    assert fetch == guidance.HINT_FETCH

    fetch_command = _hint_command(fetch, query=query, url=url)
    assert isinstance(fetch_command, argv.FetchCommand)
    assert fetch_command.url == url
    assert fetch_command.full is False

    search_command = _hint_command(search, query=query, url=url)
    assert isinstance(search_command, argv.SearchCommand)
    assert search_command.options.query == f"{query} site:docs.python.org"
    assert search_command.options.count == 5
    assert search_command.output.fields == tuple(rows[0])


def test_readme_content_defaults_and_projection_precedence_match_parser() -> None:
    search = _section("### `deepsearch-axi search <query>`")
    projection = _section("### Counts, projection, and content")
    canonical = (
        "`--need-content` alone adds a fourth `content` column.",
        "An explicit `--fields` projection replaces the defaults and preserves the requested "
        "order, so a projection wins over `--need-content`.",
    )
    for section in (search, projection):
        for sentence in canonical:
            assert sentence in section
    assert "`--count` accepts 1-50 and defaults to 10." in search
    assert "does not change the default projection" not in README

    default = argv.parse_args(["search", "q"])
    assert isinstance(default, argv.SearchCommand)
    assert default.options.count == models.SEARCH_DEFAULT_COUNT == 10
    assert default.output.fields == models.DEFAULT_FIELDS == ("title", "url", "snippet")

    opted_in = argv.parse_args(["search", "q", "--need-content"])
    assert isinstance(opted_in, argv.SearchCommand)
    assert (
        opted_in.output.fields
        == models.DEFAULT_FIELDS_WITH_CONTENT
        == (
            "title",
            "url",
            "snippet",
            "content",
        )
    )

    explicit = argv.parse_args(
        ["search", "q", "--need-content", "--fields", "summary,date,authority"]
    )
    assert isinstance(explicit, argv.SearchCommand)
    assert explicit.options.need_content is True
    assert explicit.output.fields == ("summary", "date", "authority")

    document = documents.search_document(
        "q",
        [
            {
                "title": "t",
                "url": "u",
                "snippet": "s",
                "content": "c",
                "summary": "m",
                "date": None,
                "authority": 4,
            }
        ],
        models.OutputOptions(fields=explicit.output.fields),
        request=explicit.options,
    )
    row = document["results"][0]
    assert list(row) == ["summary", "date", "authority"]


def test_readme_origin_policy_matches_validator_and_origin_fixtures() -> None:
    section = _section("### API origin")
    assert "trusted custom hostname" in section
    assert "trusted endpoint" in section
    assert "credential recipient" in section
    assert "certificate verification" in section
    assert "HTTP is permitted only for numeric loopback" in section
    assert "`localhost` and nonnumeric hostnames are rejected for HTTP" in section
    assert "non-numeric hostnames are rejected before any credential-bearing request" not in section

    # Imported here so the documented policy is checked against the same cases
    # that exercise the validator in tests/unit/test_origin.py.
    from unit.test_origin import _ACCEPT, _REJECT

    accepted = _origin_values(_ACCEPT)
    rejected = _origin_values(_REJECT)
    documented = re.findall(r"`(https?://[^`\s]+)`", section)
    assert documented
    for value, expected in (
        ("https://api.deepseek.com", models.OFFICIAL_ORIGIN),
        ("https://example.com", "https://example.com"),
        ("https://sub.example.com:443/", "https://sub.example.com:443"),
        ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
        ("http://[::1]:8123", "http://[::1]:8123"),
    ):
        assert value in accepted
        assert validate_base_url(value) == expected
    for value in documented:
        assert validate_base_url(value) == value.rstrip("/")

    for value in ("http://example.com", "http://localhost:8123"):
        assert value in rejected
        with pytest.raises(models.DeepsearchError):
            validate_base_url(value)


def test_readme_has_copyable_project_instruction_and_worked_example() -> None:
    assert guidance.PROJECT_INSTRUCTION in README
    example = _section("## Worked example")
    assert "illustrative" in example.lower()
    assert "not a live call" in example
    assert "--fields title,url,summary,date,authority" in example
    assert "truncated" in example
    assert "--full" in example
    assert "<url>" in example
    assert "selects the returned URL" in example
    assert "only if" in example


def test_readme_lists_all_ten_axi_principles() -> None:
    labels = re.findall(r"^\d+\. \*\*(.+?)\*\*", _section("## AXI principles"), re.MULTILINE)
    assert labels == AXI_PRINCIPLES


def test_license_is_the_full_mit_text_for_derek_baron() -> None:
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 Derek Baron" in license_text
    assert license_text.split() == CANONICAL_MIT.split()


def test_gitignore_ignores_secrets_state_caches_and_builds() -> None:
    ignored = (
        ".env",
        ".env.local",
        "secrets/token.txt",
        "server.pem",
        "deploy.key",
        "__pycache__/module.pyc",
        ".pytest_cache/v/cache",
        ".ruff_cache/content",
        ".deepsearch-axi/manifest.json",
        "wheelhouse/deepsearch_axi.whl",
        "build/lib/module.py",
        "dist/deepsearch_axi.whl",
        "src/deepsearch_axi.egg-info/PKG-INFO",
        ".agentfield-out-example/output.json",
        ".worktrees/example/file",
        ".DS_Store",
    )
    for path in ignored:
        assert _ignored(path), f"{path} should be ignored"
    for path in (
        ".env.example",
        ".agents/skills/deepsearch-axi/SKILL.md",
        "src/deepsearch_axi/templates/opencode_plugin.js",
        "README.md",
    ):
        assert not _ignored(path), f"{path} should remain tracked"


def test_no_forbidden_instruction_secret_or_build_files_are_tracked() -> None:
    offenders = [path for path in _tracked() if _forbidden(path)]
    assert offenders == []


def test_tracked_files_contain_no_live_secret_material() -> None:
    patterns = (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
        re.compile(r"sk-[A-Za-z0-9]{32,}"),
        re.compile(r"xox[bpoa]-[A-Za-z0-9-]{20,}"),
    )
    offenders: list[tuple[str, str]] = []
    for path in _tracked():
        data = (REPO_ROOT / path).read_bytes()
        if b"\0" in data[:4096]:
            continue
        text = data.decode("utf-8", errors="ignore")
        for pattern in patterns:
            if pattern.search(text):
                offenders.append((path, pattern.pattern))
    assert offenders == []
