"""Native session payload validation, privacy and lifecycle behavior."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

import deepsearch_axi
from deepsearch_axi.manifest import Manifest, manifest_bytes
from deepsearch_axi.models import (
    OFFICIAL_ORIGIN,
    Activity,
    CredentialStatus,
    DeepsearchError,
    LocalContext,
    PriorEvent,
)
from deepsearch_axi.session import handle_session, normalize_event, read_native_payload
from deepsearch_axi.state import STATE_RELATIVE, record_research, session_hash

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "native"


def install_manifest(root: Path) -> Path:
    tool_dir = root / ".deepsearch-axi"
    tool_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        project=str(root.resolve()),
        executable="/usr/local/bin/deepsearch-axi",
        command=("deepsearch-axi",),
        hosts={},
    )
    (tool_dir / "manifest.json").write_bytes(manifest_bytes(manifest))
    return root


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return install_manifest(root)


@pytest.fixture
def documents(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("deepsearch_axi.documents")
    calls: dict[str, list] = {"ambient": [], "end": []}

    def ambient_document(context: LocalContext, agent: str) -> dict:
        calls["ambient"].append((context, agent))
        return {"ambient": {"agent": agent}}

    def session_end_document(kind: str, recorded: bool) -> dict:
        calls["end"].append((kind, recorded))
        return {"session": {"event": kind, "recorded": recorded}}

    module.ambient_document = ambient_document
    module.session_end_document = session_end_document
    module.calls = calls
    monkeypatch.setitem(sys.modules, "deepsearch_axi.documents", module)
    monkeypatch.setattr(deepsearch_axi, "documents", module, raising=False)
    return module


def native_bytes(name: str, cwd: Path) -> bytes:
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return text.replace("__CWD__", str(cwd)).encode("utf-8")


def native_payload(name: str, cwd: Path) -> dict:
    return json.loads(native_bytes(name, cwd))


def context_for(cwd: Path) -> LocalContext:
    return LocalContext(
        bin="/usr/local/bin/deepsearch-axi",
        cwd=str(cwd),
        auth=CredentialStatus("missing", "none"),
        origin=OFFICIAL_ORIGIN,
        origin_state="ready",
        integrations=(
            ("claude-code", "missing"),
            ("codex", "missing"),
            ("opencode", "missing"),
        ),
        prior=PriorEvent(agent="opencode", event="checkpoint", sessions=99),
        activity=Activity(search_ok=99),
    )


def _state(root: Path) -> dict:
    return json.loads((root / STATE_RELATIVE).read_text(encoding="utf-8"))


class _TtyStream(io.BytesIO):
    def isatty(self) -> bool:
        return True


def test_read_native_payload_accepts_exactly_65536_bytes() -> None:
    prefix = b'{"session_id":"s","cwd":"/tmp","padding":"'
    suffix = b'"}'
    data = prefix + b"x" * (65536 - len(prefix) - len(suffix)) + suffix
    assert len(data) == 65536
    payload = read_native_payload(io.BytesIO(data))
    assert payload["session_id"] == "s"


def test_payload_errors_never_echo_payload_text() -> None:
    marker = b"SECRET-PAYLOAD-TEXT"
    with pytest.raises(DeepsearchError) as excinfo:
        read_native_payload(io.BytesIO(b"{" + marker))
    assert marker.decode() not in excinfo.value.failure.message


def test_read_native_payload_accepts_a_native_fixture(tmp_path: Path) -> None:
    payload = read_native_payload(io.BytesIO(native_bytes("claude_session_start.json", tmp_path)))
    assert payload["session_id"] == "claude-session-0001"
    assert payload["cwd"] == str(tmp_path)


@pytest.mark.parametrize(
    "stream, reason",
    [
        (None, "stdin is not piped"),
        (_TtyStream(b"{}"), "stdin is not piped"),
        (io.BytesIO(b'{"session_id":"x","cwd":"/tmp"}' + b" " * 65536), "payload exceeds"),
        (io.BytesIO(b'{"session_id": "\xff"}'), "payload is not valid UTF-8"),
        (io.BytesIO(b"not json"), "payload is not valid JSON"),
        (io.BytesIO(b""), "payload is not valid JSON"),
        (io.BytesIO(b"[]"), "payload is not a JSON object"),
        (io.BytesIO(b"null"), "payload is not a JSON object"),
        (io.BytesIO(b'"text"'), "payload is not a JSON object"),
        (io.BytesIO(b"42"), "payload is not a JSON object"),
        (io.BytesIO(b"[" * 30000 + b"]" * 30000), "payload is not valid JSON"),
        (io.StringIO("{}"), "stdin is not binary"),
    ],
)
def test_read_native_payload_rejects_adversarial_input(stream: object, reason: str) -> None:
    with pytest.raises(DeepsearchError) as excinfo:
        read_native_payload(stream)  # type: ignore[arg-type]
    failure = excinfo.value.failure
    assert failure.code == "invalid_event"
    assert reason in failure.message
    assert 1 <= len(failure.help) <= 2


@pytest.mark.parametrize(
    "name",
    [
        "claude_session_start.json",
        "claude_session_end.json",
        "codex_session_start.json",
        "codex_session_end.json",
    ],
)
def test_normalize_event_accepts_the_frozen_native_fixtures(project: Path, name: str) -> None:
    agent = "claude-code" if name.startswith("claude") else "codex"
    event = "start" if "start" in name else "end"
    payload = native_payload(name, project)
    native = normalize_event(event, agent, project, payload)
    assert native.session_hash == session_hash(agent, payload["session_id"])
    assert native.scope == "."
    assert native.kind == event


def test_normalize_event_scope_is_project_relative(project: Path) -> None:
    child = project / "src" / "pkg"
    child.mkdir(parents=True)
    native = normalize_event(
        "start", "codex", project, {"session_id": "session-1", "cwd": str(child)}
    )
    assert native.scope == "src/pkg"


@pytest.mark.parametrize(
    "hook_event_name, expected",
    [
        ("SessionEnd", "end"),
        (None, "end"),
        ("session.created", "end"),
        ("session.idle", "checkpoint"),
        ("plugin.dispose", "checkpoint"),
    ],
)
def test_normalize_event_maps_opencode_checkpoints(
    project: Path, hook_event_name: str | None, expected: str
) -> None:
    payload: dict = {"session_id": "session-1", "cwd": str(project)}
    if hook_event_name is not None:
        payload["hook_event_name"] = hook_event_name
    assert normalize_event("end", "opencode", project, payload).kind == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": None},
        {"session_id": 3},
        {"session_id": ""},
        {"session_id": "x" * 513},
        {"session_id": "../escape"},
        {"session_id": ".."},
        {"session_id": "a/b"},
        {"session_id": "a\\b"},
        {"session_id": "a\x00b"},
        {"session_id": "a\nb"},
        {"session_id": "\ud800"},
        {"cwd": None},
        {"cwd": 42},
        {"cwd": ""},
        {"cwd": "relative/dir"},
        {"cwd": "/tmp/\x00evil"},
        {"cwd": "/tmp/\ud800evil"},
        {"cwd": "__OUTSIDE__"},
        {"cwd": "__SYMLINK__"},
    ],
)
def test_normalize_event_rejects_invalid_payloads(
    project: Path, tmp_path: Path, overrides: dict
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = project / "escape"
    symlink.symlink_to(outside)
    payload: dict = {"session_id": "session-1", "cwd": str(project)}
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        elif value == "__OUTSIDE__":
            payload[key] = str(outside)
        elif value == "__SYMLINK__":
            payload[key] = str(symlink)
        else:
            payload[key] = value
    with pytest.raises(DeepsearchError) as excinfo:
        normalize_event("start", "codex", project, payload)
    assert excinfo.value.failure.code == "invalid_event"


def test_normalize_event_rejects_a_non_directory_project(tmp_path: Path) -> None:
    file_root = tmp_path / "file-root"
    file_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(DeepsearchError) as excinfo:
        normalize_event(
            "start",
            "codex",
            file_root,
            {"session_id": "session-1", "cwd": str(tmp_path)},
        )
    assert excinfo.value.failure.code == "invalid_event"


def test_handle_session_start_records_and_returns_ambient(
    project: Path, documents: types.ModuleType
) -> None:
    payload = native_payload("claude_session_start.json", project)
    seen: list[Path] = []

    def context(cwd: Path) -> LocalContext:
        seen.append(cwd)
        return context_for(cwd)

    result = handle_session("start", "claude-code", project, payload, context_for=context)
    assert result.recorded is True
    assert result.document == {"ambient": {"agent": "claude-code"}}
    assert seen == [project.resolve()]
    assert len(documents.calls["ambient"]) == 1
    built_context, agent = documents.calls["ambient"][0]
    assert agent == "claude-code"
    assert built_context.prior is None
    assert built_context.activity is None
    state = _state(project)
    assert state["sessions"] == [
        {
            "id": session_hash("claude-code", payload["session_id"]),
            "agent": "claude-code",
            "scope": ".",
            "event": "start",
            "seq": 1,
        }
    ]


def test_handle_session_start_succeeds_with_a_missing_key(
    project: Path, documents: types.ModuleType
) -> None:
    payload = native_payload("codex_session_start.json", project)
    result = handle_session("start", "codex", project, payload, context_for=context_for)
    assert result.recorded is True
    built_context, _ = documents.calls["ambient"][0]
    assert built_context.auth.state == "missing"


def test_handle_session_start_uses_resolved_cwd_for_context(
    project: Path, documents: types.ModuleType
) -> None:
    target = project / "real"
    target.mkdir()
    link = project / "link"
    link.symlink_to(target)
    seen: list[Path] = []

    def context(cwd: Path) -> LocalContext:
        seen.append(cwd)
        return context_for(cwd)

    payload = {"session_id": "session-1", "cwd": str(link)}
    result = handle_session("start", "codex", project, payload, context_for=context)
    assert result.recorded is True
    assert seen == [target.resolve()]
    assert _state(project)["sessions"][0]["scope"] == "real"


def test_handle_session_end_is_idempotent(project: Path, documents: types.ModuleType) -> None:
    payload = native_payload("codex_session_end.json", project)
    first = handle_session("end", "codex", project, payload, context_for=context_for)
    state_path = project / STATE_RELATIVE
    before = state_path.read_bytes()
    second = handle_session("end", "codex", project, payload, context_for=context_for)
    assert first.recorded is True
    assert second.recorded is False
    assert state_path.read_bytes() == before
    assert documents.calls["end"] == [("end", True), ("end", False)]
    assert _state(project)["seq"] == 1


def test_handle_session_end_checkpoint_is_idempotent(
    project: Path, documents: types.ModuleType
) -> None:
    payload = {
        "session_id": "opencode-session-1",
        "cwd": str(project),
        "hook_event_name": "session.idle",
    }
    first = handle_session("end", "opencode", project, payload, context_for=context_for)
    second = handle_session("end", "opencode", project, payload, context_for=context_for)
    assert first.recorded is True
    assert second.recorded is False
    assert documents.calls["end"] == [("checkpoint", True), ("checkpoint", False)]
    assert _state(project)["sessions"][0]["event"] == "checkpoint"


def test_handle_session_start_consumes_prior_end_and_exact_scope(
    project: Path, documents: types.ModuleType
) -> None:
    end_payload = native_payload("codex_session_end.json", project)
    assert (
        handle_session("end", "codex", project, end_payload, context_for=context_for).recorded
        is True
    )
    sibling = project / "sibling"
    sibling.mkdir()
    record_research(project, ".", "fetch", True)
    record_research(project, "sibling", "search", True)

    start_payload = native_payload("claude_session_start.json", project)
    result = handle_session("start", "claude-code", project, start_payload, context_for=context_for)
    assert result.recorded is True
    built_context, _ = documents.calls["ambient"][0]
    assert built_context.prior == PriorEvent(agent="codex", event="end", sessions=2)
    assert built_context.activity == Activity(search_ok=0, search_fail=0, fetch_ok=1, fetch_fail=0)


def test_handle_session_without_a_manifest_creates_no_state(
    tmp_path: Path, documents: types.ModuleType
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    start = handle_session(
        "start",
        "opencode",
        root,
        {"session_id": "session-1", "cwd": str(root)},
        context_for=context_for,
    )
    end = handle_session(
        "end",
        "opencode",
        root,
        {"session_id": "session-1", "cwd": str(root)},
        context_for=context_for,
    )
    assert start.recorded is False
    assert end.recorded is False
    assert not (root / ".deepsearch-axi").exists()
    assert len(documents.calls["ambient"]) == 1
    assert documents.calls["end"] == [("end", False)]


def test_handle_session_invalid_payload_writes_no_state(
    project: Path, documents: types.ModuleType
) -> None:
    with pytest.raises(DeepsearchError) as excinfo:
        handle_session(
            "start",
            "codex",
            project,
            {"cwd": str(project)},
            context_for=context_for,
        )
    assert excinfo.value.failure.code == "invalid_event"
    assert not (project / STATE_RELATIVE).exists()
    assert documents.calls == {"ambient": [], "end": []}


def test_handle_session_persists_no_private_payload_fields(
    project: Path, documents: types.ModuleType, tmp_path: Path
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("SECRET-TRANSCRIPT-CONTENT", encoding="utf-8")
    payload = {
        "session_id": "raw-session-private-0001",
        "cwd": str(project),
        "transcript_path": str(transcript),
        "hook_event_name": "SessionStart",
        "query": "PRIVATE-QUERY",
        "content": "PRIVATE-CONTENT",
        "api_key": "sk-PRIVATE-KEY",
        "prompt": "PRIVATE-PROMPT",
        "nested": {"secret": "PRIVATE-NESTED"},
    }
    handle_session("start", "claude-code", project, payload, context_for=context_for)

    persisted = b"".join(
        path.read_bytes()
        for path in sorted((project / ".deepsearch-axi").rglob("*"))
        if path.is_file()
    )
    for private in (
        "raw-session-private-0001",
        "transcript.jsonl",
        "SECRET-TRANSCRIPT-CONTENT",
        "PRIVATE-QUERY",
        "PRIVATE-CONTENT",
        "sk-PRIVATE-KEY",
        "PRIVATE-PROMPT",
        "PRIVATE-NESTED",
    ):
        assert private.encode("utf-8") not in persisted
    assert session_hash("claude-code", payload["session_id"]).encode("utf-8") in persisted
    assert transcript.read_text(encoding="utf-8") == "SECRET-TRANSCRIPT-CONTENT"
