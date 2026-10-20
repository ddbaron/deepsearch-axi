"""Foundation contract tests: failures, guidance, fsutil and manifest."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import toon_format
from support.cli import decode, run_cli

from deepsearch_axi import failures, fsutil, guidance, models
from deepsearch_axi._version import VERSION
from deepsearch_axi.failures import (
    internal_error,
    invalid_base_url,
    invalid_event,
    invalid_key_config,
    local_io,
    missing_executable,
    missing_key,
    response_too_large,
    setup_conflict,
    status_failure,
    timeout_failure,
    transport_failure,
    usage_failure,
)
from deepsearch_axi.fsutil import (
    apply_changes,
    atomic_write,
    ensure_within_root,
    read_bytes_or_none,
)
from deepsearch_axi.manifest import (
    MANIFEST_RELATIVE,
    HostPlan,
    Manifest,
    ManifestHost,
    load_manifest,
    manifest_bytes,
)
from deepsearch_axi.models import DeepsearchError, FileChange


def _assert_failure(failure: models.Failure, code: str, status: int | None) -> None:
    assert failure.code == code
    assert failure.http_status == status
    assert 1 <= len(failure.help) <= 2
    expected_exit = models.EXIT_USAGE if code == "usage" else models.EXIT_FAILURE
    assert models.exit_code_for(failure) == expected_exit


def test_models_frozen_constants() -> None:
    assert models.AGENTS == ("claude-code", "codex", "opencode")
    assert models.OFFICIAL_ORIGIN == "https://api.deepseek.com"
    assert models.TOOL_PATHS == {
        "search": "/v1/tools/search",
        "fetch": "/v1/tools/fetch",
    }
    assert models.TIMEOUT_SECONDS == 30.0
    assert models.MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    assert models.MAX_NATIVE_PAYLOAD_BYTES == 65536
    assert models.MAX_STATE_FILE_BYTES == 65536
    assert models.MAX_SESSION_RECORDS == 20
    assert models.MAX_SCOPE_RECORDS == 20
    assert models.MAX_DOTENV_BYTES == 65536
    assert models.SEARCH_FIELDS == (
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
    assert models.DEFAULT_FIELDS == ("title", "url", "snippet")
    assert models.DEFAULT_FIELDS_WITH_CONTENT == ("title", "url", "snippet", "content")
    assert models.TEXT_LIMITS == {"title": 200, "snippet": 200, "summary": 1000, "content": 1000}
    assert models.SEARCH_DEFAULT_COUNT == 10
    assert models.QUERY_MAX == 128
    assert models.ORI_QUESTION_MAX == 128
    assert models.FETCH_URL_MAX == 8192
    assert models.REDACTED == "[REDACTED]"
    assert (models.EXIT_OK, models.EXIT_FAILURE, models.EXIT_USAGE) == (0, 1, 2)


def test_models_are_frozen_and_credentials_hide_their_value() -> None:
    failure = models.Failure("code", "message", ("help",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        failure.code = "changed"  # type: ignore[misc]
    credential = models.Credential(value="sk-test-secret-value", source="env")
    assert "sk-test-secret-value" not in repr(credential)
    assert str(credential) == "Credential(source=env)"
    assert models.CredentialStatus("configured", "env").state == "configured"


def test_usage_failure_carries_flags_and_first_example() -> None:
    failure = usage_failure(("search",), "unknown flag --foo for search")
    _assert_failure(failure, "usage", None)
    assert failure.message == "unknown flag --foo for search"
    assert failure.help[0] == (
        "valid flags for search: --count N (default 10), --need-content, "
        "--ori-question TEXT, --localization IP, --fields FIELD,... "
        "(default title,url,snippet), --full, --help"
    )
    assert failure.help[1] == guidance.COMMANDS[("search",)].examples[0]

    root_failure = usage_failure((), "unknown command or flag --nope")
    assert root_failure.help[0].startswith("valid flags for deepsearch-axi: commands: ")
    assert root_failure.help[1] == "deepsearch-axi"


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (missing_key, "missing_key"),
        (invalid_base_url, "invalid_config"),
        (missing_executable, "invalid_config"),
        (internal_error, "internal_error"),
        (lambda: invalid_key_config("env", "control_characters"), "invalid_config"),
        (lambda: invalid_key_config("cwd_dotenv", "duplicate"), "invalid_config"),
        (lambda: invalid_key_config("user_dotenv", "unmatched_quote"), "invalid_config"),
        (lambda: setup_conflict(".claude/settings.json", "not valid JSON"), "invalid_config"),
        (lambda: local_io("./.env"), "local_io"),
        (lambda: invalid_event("stdin is not piped"), "invalid_event"),
        (lambda: transport_failure("search"), "transport_error"),
        (lambda: timeout_failure("fetch"), "timeout"),
        (lambda: response_too_large("search"), "response_too_large"),
        (lambda: failures.invalid_response("fetch"), "invalid_response"),
    ],
)
def test_local_failure_table(factory, code: str) -> None:
    _assert_failure(factory(), code, None)


def test_invalid_key_config_names_source_and_reason() -> None:
    failure = invalid_key_config("cwd_dotenv", "duplicate")
    assert "./.env" in failure.message
    assert "duplicate" in failure.message
    assert "one line DEEPSEEK_API_KEY=<key>" in failure.help[0]
    assert failure.help[1] == guidance.HINT_SET_KEY


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_request"),
        (401, "unauthorized"),
        (402, "insufficient_balance"),
        (403, "forbidden"),
        (404, "not_found"),
        (408, "timeout"),
        (504, "timeout"),
        (413, "request_too_large"),
        (429, "rate_limited"),
        (500, "upstream_error"),
        (503, "upstream_error"),
        (301, "redirect_refused"),
        (308, "redirect_refused"),
        (418, "http_error"),
        (201, "http_error"),
    ],
)
def test_status_failure_table(status: int, code: str) -> None:
    for tool in ("search", "fetch"):
        failure = status_failure(status, tool)
        _assert_failure(failure, code, status)
        for item in failure.help:
            assert "sk-test" not in item
            assert "Bearer" not in item


def test_status_failure_tool_specific_guidance() -> None:
    assert "permitted query" in status_failure(403, "search").help[0]
    assert "permitted URL" in status_failure(403, "fetch").help[0]
    assert "accessible URL" in status_failure(404, "fetch").help[0]
    assert "DEEPSEEK_BASE_URL" in status_failure(404, "search").help[0]
    redirect = status_failure(302, "search")
    assert "redirect" in redirect.message
    assert "Location" not in redirect.message
    assert status_failure(400, "search").help[1] == guidance.flags_line(("search",))


def test_guidance_contract() -> None:
    assert guidance.DESCRIPTION == (
        "Agent-native CLI for DeepSeek web search and page fetch with compact TOON output."
    )
    assert guidance.PROJECT_INSTRUCTION == (
        "Use deepsearch-axi for DeepSeek web search and page fetch; run deepsearch-axi "
        "for local readiness and follow its contextual TOON hints."
    )
    assert guidance.REPO_URL == "https://github.com/ddbaron/deepsearch-axi"
    assert guidance.HINT_SEARCH == 'deepsearch-axi search "<query>"'
    assert guidance.HINT_FETCH == "deepsearch-axi fetch <url>"
    assert guidance.HINT_FETCH_FULL == "deepsearch-axi fetch <url> --full"
    assert guidance.HINT_SET_KEY == "export DEEPSEEK_API_KEY=<your-deepseek-api-key>"
    assert guidance.HINT_SETUP == "deepsearch-axi setup"
    assert tuple(guidance.COMMANDS) == (
        (),
        ("search",),
        ("fetch",),
        ("setup",),
        ("session",),
        ("session", "start"),
        ("session", "end"),
    )
    assert guidance.flags_line(("search",)) == (
        "--count N (default 10), --need-content, --ori-question TEXT, "
        "--localization IP, --fields FIELD,... (default title,url,snippet), --full, --help"
    )
    assert guidance.flags_line(()) == (
        "commands: search, fetch, setup, session; flags: --help, -v|-V|--version (alone)"
    )
    search = guidance.COMMANDS[("search",)]
    assert [flag.name for flag in search.flags] == [
        "--count",
        "--need-content",
        "--ori-question",
        "--localization",
        "--fields",
        "--full",
    ]
    assert search.flags[4].choices == models.SEARCH_FIELDS
    assert guidance.COMMANDS[("setup",)].flags[0].choices == (
        "claude-code",
        "codex",
        "opencode",
        "all",
    )
    assert guidance.COMMANDS[("session", "start")].flags[0].default == "required"
    assert guidance.COMMANDS[()].flags[0].name == "-v, -V, --version"
    assert guidance.flags_line(("session", "start")) == ("--agent NAME, --project ABS_DIR, --help")


def test_guidance_imports_no_package_modules(env: dict[str, str], tmp_path: Path) -> None:
    code = "\n".join(
        [
            "import sys",
            "import deepsearch_axi.guidance",
            "bad = sorted(",
            "    name for name in sys.modules",
            "    if name.startswith('deepsearch_axi.')",
            "    and name != 'deepsearch_axi.guidance'",
            ")",
            "bad += sorted(name for name in ('toon_format', 'tomlkit') if name in sys.modules)",
            "assert not bad, bad",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


def test_skill_markdown_is_deterministic_and_live_free() -> None:
    first = guidance.skill_markdown()
    second = guidance.skill_markdown()
    assert first == second
    assert first.endswith("\n")
    assert not first.endswith("\n\n")
    lines = first.splitlines()
    assert lines[0] == "---"
    assert lines[1] == "name: deepsearch-axi"
    assert lines[2] == (
        'description: "Search the web and fetch pages as Markdown through DeepSeek with '
        "compact TOON output. Use when a task needs current web results, source URLs, or "
        'the contents of a specific page."'
    )
    assert lines[3] == "---"
    assert (
        "uvx --from git+https://github.com/ddbaron/deepsearch-axi.git "
        'deepsearch-axi search "<query>"' in first
    )
    assert guidance.PROJECT_INSTRUCTION in first
    assert "bin:" not in first
    assert "cwd:" not in first


def test_atomic_write_creates_replaces_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    atomic_write(target, b"first")
    assert target.read_bytes() == b"first"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    target.chmod(0o640)
    atomic_write(target, b"second")
    assert target.read_bytes() == b"second"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    leftovers = [
        entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".deepsearch-axi-")
    ]
    assert leftovers == []


def test_atomic_write_removes_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.json"

    def fail_replace(src, dst) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(fsutil.os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write(target, b"payload")
    assert not target.exists()
    leftovers = [
        entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".deepsearch-axi-")
    ]
    assert leftovers == []


def test_apply_changes_rolls_back_prior_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "one" / "first.txt"
    first.parent.mkdir()
    first.write_bytes(b"before")
    second = tmp_path / "two" / "second.txt"
    changes = (
        FileChange(path=first, before=b"before", after=b"after"),
        FileChange(path=second, before=None, after=b"created"),
    )
    real_replace = fsutil.os.replace

    def failing_replace(src, dst) -> None:
        if Path(dst) == second:
            raise OSError("injected write failure")
        real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", failing_replace)
    with pytest.raises(DeepsearchError) as info:
        apply_changes(changes)
    assert info.value.failure.code == "local_io"
    assert first.read_bytes() == b"before"
    assert not second.exists()
    assert not second.parent.exists()


def test_apply_changes_applies_in_order(tmp_path: Path) -> None:
    changes = (
        FileChange(path=tmp_path / "a" / "one.txt", before=None, after=b"1"),
        FileChange(path=tmp_path / "b" / "two.txt", before=None, after=b"2"),
    )
    apply_changes(changes)
    assert (tmp_path / "a" / "one.txt").read_bytes() == b"1"
    assert (tmp_path / "b" / "two.txt").read_bytes() == b"2"


def test_ensure_within_root_accepts_inside_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "nested").mkdir(parents=True)
    assert ensure_within_root(root, "nested/file.txt") == root / "nested/file.txt"
    (root / "real").mkdir()
    (root / "inside-link").symlink_to(root / "real", target_is_directory=True)
    assert ensure_within_root(root, "inside-link/file.txt") == root / "inside-link/file.txt"
    (root / "existing.txt").write_text("x", encoding="utf-8")
    (root / "inside-file-link").symlink_to(root / "existing.txt")
    with pytest.raises(DeepsearchError):
        ensure_within_root(root, "inside-file-link")


def test_ensure_within_root_rejects_escapes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "dir-link").symlink_to(outside, target_is_directory=True)
    (root / "file-link").symlink_to(outside / "target.txt")
    for relative in (
        "../escape.txt",
        "nested/../../escape.txt",
        str(outside / "absolute.txt"),
        "dir-link/file.txt",
        "file-link",
    ):
        with pytest.raises(DeepsearchError) as info:
            ensure_within_root(root, relative)
        assert info.value.failure.code == "invalid_config"
        assert "symlink or path escapes the project" in info.value.failure.message


def test_read_bytes_or_none(tmp_path: Path) -> None:
    assert read_bytes_or_none(tmp_path / "missing.txt") is None
    present = tmp_path / "present.txt"
    present.write_bytes(b"data")
    assert read_bytes_or_none(present) == b"data"
    with pytest.raises(DeepsearchError) as info:
        read_bytes_or_none(tmp_path)
    assert info.value.failure.code == "local_io"


def test_locked_excludes_other_processes(tmp_path: Path, env: dict[str, str]) -> None:
    lock_path = tmp_path / "locks" / "state.lock"
    script = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "from deepsearch_axi.fsutil import locked",
            "with locked(Path(sys.argv[1])):",
            "    print('locked', flush=True)",
            "    sys.stdin.readline()",
            "print('released', flush=True)",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        fd = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=10)
    assert process.returncode == 0


def _manifest(root: Path) -> Manifest:
    return Manifest(
        project=str(root.resolve()),
        executable="/opt/deepsearch-axi/bin/deepsearch-axi",
        command=("deepsearch-axi",),
        hosts={
            "opencode": ManifestHost(
                files=(".opencode/plugins/deepsearch-axi.js",),
                commands=('["./deepsearch-axi"]',),
            ),
            "claude-code": ManifestHost(
                files=(".claude/settings.json",),
                commands=('deepsearch-axi session start --agent claude-code --project "/p"',),
            ),
        },
    )


def test_manifest_round_trip_and_determinism(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    manifest = _manifest(root)
    first = manifest_bytes(manifest)
    second = manifest_bytes(manifest)
    assert first == second
    assert first.endswith(b"\n")
    payload = json.loads(first)
    assert list(payload) == [
        "owner",
        "schema",
        "version",
        "project",
        "executable",
        "command",
        "hosts",
    ]
    assert list(payload["hosts"]) == ["claude-code", "opencode"]
    assert payload["version"] == "0.1.0"

    manifest_path = root / MANIFEST_RELATIVE
    manifest_path.parent.mkdir()
    manifest_path.write_bytes(first)
    loaded = load_manifest(root)
    assert loaded == manifest
    assert manifest_bytes(loaded) == first


def test_manifest_empty_hosts_and_missing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    assert load_manifest(root) is None
    empty = Manifest(
        project=str(root.resolve()),
        executable="/bin/deepsearch-axi",
        command=("deepsearch-axi",),
        hosts={},
    )
    data = manifest_bytes(empty)
    assert json.loads(data)["hosts"] == {}
    manifest_path = root / MANIFEST_RELATIVE
    manifest_path.parent.mkdir()
    manifest_path.write_bytes(data)
    assert load_manifest(root) == empty


def _valid_payload(root: Path) -> dict:
    return {
        "owner": "deepsearch-axi",
        "schema": 1,
        "version": "0.1.0",
        "project": str(root.resolve()),
        "executable": "/bin/deepsearch-axi",
        "command": ["deepsearch-axi"],
        "hosts": {},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(owner="other-tool"),
        lambda payload: payload.update(schema=2),
        lambda payload: payload.update(project="/some/other/project"),
        lambda payload: payload.pop("command"),
        lambda payload: payload.pop("executable"),
        lambda payload: payload.update(hosts=[]),
        lambda payload: payload.update(hosts={"codex": {"files": "x", "commands": []}}),
    ],
)
def test_manifest_rejects_invalid_payloads(tmp_path: Path, mutate) -> None:
    root = tmp_path / "project"
    root.mkdir()
    payload = _valid_payload(root)
    mutate(payload)
    manifest_path = root / MANIFEST_RELATIVE
    manifest_path.parent.mkdir()
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DeepsearchError) as info:
        load_manifest(root)
    assert info.value.failure.code == "invalid_config"
    assert info.value.failure.help[0].endswith("re-run deepsearch-axi setup")


def test_manifest_rejects_malformed_json(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    manifest_path = root / MANIFEST_RELATIVE
    manifest_path.parent.mkdir()
    manifest_path.write_bytes(b'{"owner": ')
    with pytest.raises(DeepsearchError) as info:
        load_manifest(root)
    assert info.value.failure.code == "invalid_config"


def test_host_plan_dataclass_shape() -> None:
    record = ManifestHost(files=(".claude/settings.json",), commands=())
    plan = HostPlan(changes=(), record=record)
    assert plan.changes == ()
    assert plan.record is record


def test_run_cli_and_decode(env: dict[str, str], tmp_path: Path) -> None:
    completed = run_cli(["--version"], env=env, cwd=tmp_path)
    assert completed.code == 0
    assert completed.stdout == VERSION + "\n"
    assert completed.stderr == ""
    payload = {"query": "asyncio", "count": 2, "results": [{"title": "T", "url": "https://e"}]}
    assert decode(toon_format.encode(payload)) == payload


NATIVE_FIXTURES = (
    ("claude_session_start.json", "SessionStart"),
    ("claude_session_end.json", "SessionEnd"),
    ("codex_session_start.json", "SessionStart"),
    ("codex_session_end.json", "SessionEnd"),
)


@pytest.mark.parametrize(("name", "hook_event_name"), NATIVE_FIXTURES)
def test_native_fixtures_are_frozen_and_placeholders(name: str, hook_event_name: str) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "native" / name
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert payload["cwd"] == "__CWD__"
    assert payload["hook_event_name"] == hook_event_name
    assert isinstance(payload["session_id"], str) and payload["session_id"]
    assert isinstance(payload["transcript_path"], str) and payload["transcript_path"]
