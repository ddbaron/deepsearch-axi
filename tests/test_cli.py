"""AC-21: complete usage grammar, safe TOON errors and a no-prompt boundary.

The corpus drives the real CLI with closed stdin against an observed loopback
stub and an audit hook that records credential-file opens. Every rejected
invocation must exit 2 before any file read or request; a positive control
proves the read instrumentation actually observes credential reads.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from support.cli import decode, run_cli
from support.env import FAKE_KEY
from support.stub_server import StubServer, json_response

USER_DOTENV = Path(".config") / "deepsearch-axi" / ".env"
SEARCH_PAYLOAD = {
    "results": [
        {
            "title": "Control",
            "url": "https://example.com/control",
            "snippet": "Control snippet",
            "summary": "",
            "content": "",
            "authority": -1,
            "rerank_score": 0.0,
        }
    ]
}

# (argv, affected command label, required message fragment)
_USAGE_CASES: tuple[tuple[list[str], str, str], ...] = (
    (["search"], "search", "missing <query> for search"),
    (["fetch"], "fetch", "missing <url> for fetch"),
    (["fetch", "--full"], "fetch", "missing <url> for fetch"),
    (["search", "one", "two"], "search", "too many arguments for search"),
    (
        ["fetch", "https://example.com/", "extra"],
        "fetch",
        "too many arguments for fetch",
    ),
    (["setup", "extra"], "setup", "too many arguments for setup"),
    (["bogus"], "deepsearch-axi", "unknown command or flag"),
    (["search", "q", "--bogus"], "search", "unknown flag --bogus"),
    (["search", "q", "--json"], "search", "unknown flag --json"),
    (["search", "q", "--count", FAKE_KEY], "search", "invalid value for --count"),
    (["search", "q", "--api-key", FAKE_KEY], "search", "unknown flag --api-key"),
    (
        ["search", "q", f"--localization={FAKE_KEY}"],
        "search",
        "invalid value for --localization",
    ),
    (["search", "q", "--fields", FAKE_KEY], "search", "invalid value for --fields"),
    (["fetch", f"ftp://example.com/{FAKE_KEY}"], "fetch", "invalid value for <url>"),
    (
        ["session", "start", "--agent", FAKE_KEY, "--project", "/tmp"],
        "session start",
        "invalid value for --agent",
    ),
    (["search", "q", "--full", "--full"], "search", "repeated flag --full"),
    (["fetch", "https://example.com/", "--json"], "fetch", "unknown flag --json"),
    (["search", "q", "--cou", "2"], "search", "unknown flag --cou"),
    (["fetch", "https://example.com/", "--ful"], "fetch", "unknown flag --ful"),
    (
        ["session", "start", "--ag", "codex", "--project", "/tmp"],
        "session start",
        "unknown flag --ag",
    ),
    (["search", "q", "--count", "1", "--count", "2"], "search", "repeated flag --count"),
    (
        ["search", "q", "--fields", "title", "--fields", "url"],
        "search",
        "repeated flag --fields",
    ),
    (["search", "q", "--count"], "search", "missing value for --count"),
    (["search", "q", "--ori-question"], "search", "missing value for --ori-question"),
    (["search", "q", "--count", "--full"], "search", "missing value for --count"),
    (["setup", "--agent"], "setup", "missing value for --agent"),
    (["fetch", "https://example.com/", "--full=yes"], "fetch", "invalid value for --full"),
    (["search", "q", "--help=1"], "search", "invalid value for --help"),
    (["session"], "session", "missing <start|end> for session"),
    (["session", "bogus"], "session", "unknown subcommand for session"),
    (["session", "start"], "session start", "missing --agent for session start"),
    (
        ["session", "start", "--agent", "codex"],
        "session start",
        "missing --project for session start",
    ),
    (["session", "end", "--agent", "codex"], "session end", "missing --project for session end"),
    (
        ["session", "start", "--agent", "other", "--project", "/tmp"],
        "session start",
        "invalid value for --agent",
    ),
    (["setup", "--agent", "other"], "setup", "invalid value for --agent"),
    (["search", "q", "--version"], "search", "only valid alone"),
    (["fetch", "https://example.com/", "-v"], "fetch", "only valid alone"),
    (["setup", "--version"], "setup", "only valid alone"),
    (
        ["session", "start", "--agent", "codex", "--project", "/tmp", "--version"],
        "session start",
        "only valid alone",
    ),
    (["session", "--version"], "session", "only valid alone"),
    (["--version", "extra"], "deepsearch-axi", "only valid alone"),
    (["-V", "--version"], "deepsearch-axi", "only valid alone"),
    (["search", "--help", "--bogus"], "search", "unknown flag --bogus"),
    (
        ["fetch", "--help", "https://example.com/", "extra"],
        "fetch",
        "too many arguments for fetch",
    ),
    (["session", "start", "--help", "--bogus"], "session start", "unknown flag --bogus"),
)

_HELP_CASES: tuple[tuple[list[str], str], ...] = (
    (["search", "--help"], "deepsearch-axi search"),
    (["fetch", "--help"], "deepsearch-axi fetch"),
    (["setup", "--help"], "deepsearch-axi setup"),
    (["session", "--help"], "deepsearch-axi session"),
    (["session", "start", "--help"], "deepsearch-axi session start"),
    (["session", "end", "--help"], "deepsearch-axi session end"),
)


def _watched_paths(project: Path, home: Path) -> list[Path]:
    return [project / ".env", home / USER_DOTENV]


def test_usage_and_no_prompts(
    stub: Callable[..., StubServer], env: dict[str, str], home_dir: Path, project_dir: Path
) -> None:
    server = stub(lambda request: json_response(SEARCH_PAYLOAD))
    cwd_dotenv = project_dir / ".env"
    home_dotenv = home_dir / USER_DOTENV
    cwd_dotenv.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    home_dotenv.parent.mkdir(parents=True)
    home_dotenv.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    environment = dict(env, DEEPSEEK_BASE_URL=server.base_url)
    watched = _watched_paths(project_dir, home_dir)

    # Positive control: the instrumentation really observes credential reads.
    control = run_cli(
        ["search", "instrumentation control"],
        env=environment,
        cwd=project_dir,
        read_guard=watched,
    )
    assert control.code == 0, control.stderr
    assert cwd_dotenv.resolve() in control.reads
    assert home_dotenv.resolve() not in control.reads

    for args, label, fragment in _USAGE_CASES:
        before = len(server.requests)
        run = run_cli(args, env=environment, cwd=project_dir, read_guard=watched)
        assert run.code == 2, (args, run.stdout, run.stderr)
        assert run.stderr == ""
        document = decode(run.stdout)
        assert document["error"]["code"] == "usage"
        assert fragment in document["error"]["message"]
        assert 1 <= len(document["help"]) <= 2
        assert document["help"][0].startswith(f"valid flags for {label}:")
        assert "--help" in document["help"][0]
        assert document["help"][1].startswith("deepsearch-axi")
        assert len(server.requests) == before
        assert run.reads == ()
        assert FAKE_KEY not in run.stdout + run.stderr

    # Recognized help wins over a missing positional, but never over unknown
    # tokens (the --bogus cases above) or too many positionals.
    for args, command in _HELP_CASES:
        before = len(server.requests)
        run = run_cli(args, env=environment, cwd=project_dir, read_guard=watched)
        assert run.code == 0, (args, run.stdout, run.stderr)
        assert run.stderr == ""
        document = decode(run.stdout)
        assert document["command"] == command
        assert len(document["examples"]) in (2, 3)
        assert len(server.requests) == before
        assert run.reads == ()
        assert FAKE_KEY not in run.stdout + run.stderr

    # Closed/EOF stdin never prompts or hangs: EOF is accepted as a normal
    # closed descriptor for both help and rejected usage.
    for args, expected_code in (
        (["search", "--help"], 0),
        (["search"], 2),
        (["session", "start", "--help"], 0),
    ):
        run = run_cli(
            args,
            env=environment,
            cwd=project_dir,
            stdin=b"",
            timeout=20,
            read_guard=watched,
        )
        assert run.code == expected_code, (args, run.stdout, run.stderr)
        assert run.stderr == ""
        assert decode(run.stdout)
        assert run.reads == ()
        lowered = (run.stdout + run.stderr).lower()
        for prompt in ("continue?", "password", "press enter", "input()"):
            assert prompt not in lowered
