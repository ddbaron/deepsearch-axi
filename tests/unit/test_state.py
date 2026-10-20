"""Bounded workspace state: hashing, scopes, counters, eviction and locking."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from deepsearch_axi.manifest import Manifest, manifest_bytes
from deepsearch_axi.models import (
    MAX_SESSION_RECORDS,
    MAX_STATE_FILE_BYTES,
    Activity,
    NativeEvent,
    PriorEvent,
)
from deepsearch_axi.state import (
    STATE_RELATIVE,
    find_state_root,
    read_view,
    record_research,
    record_session_event,
    scope_for,
    session_hash,
)


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


def _state_path(root: Path) -> Path:
    return root / STATE_RELATIVE


def _load_state(root: Path) -> dict:
    return json.loads(_state_path(root).read_text(encoding="utf-8"))


def _seed_state(root: Path, state: dict) -> None:
    _state_path(root).write_text(json.dumps(state), encoding="utf-8")


def _event(
    session: str, kind: str = "start", scope: str = ".", agent: str = "codex"
) -> NativeEvent:
    return NativeEvent(session_hash=session_hash(agent, session), scope=scope, kind=kind)


def test_session_hash_is_a_stable_opaque_key() -> None:
    expected = hashlib.sha256(b"codex\0codex-session-0001").hexdigest()[:32]
    hashed = session_hash("codex", "codex-session-0001")
    assert hashed == expected
    assert len(hashed) == 32
    assert all(character in "0123456789abcdef" for character in hashed)
    assert session_hash("claude-code", "codex-session-0001") != hashed
    assert session_hash("codex", "codex-session-0002") != hashed


def test_scope_for_root_child_outside_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "a" / "b"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    (root / "inside").symlink_to(child)
    assert scope_for(root, root) == "."
    assert scope_for(root, child) == "a/b"
    assert scope_for(root, child / "..") == "a"
    assert scope_for(root, outside) is None
    assert scope_for(root, root / "escape") is None
    assert scope_for(root, root / "inside") == "a/b"


def test_scope_for_hashes_very_long_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    deep = root / ("a" * 150) / ("b" * 150)
    deep.mkdir(parents=True)
    scope = scope_for(root, deep)
    assert scope is not None
    assert scope.startswith("h:")
    assert len(scope) == 34
    assert all(character in "0123456789abcdef" for character in scope[2:])


def test_find_state_root_walks_to_nearest_manifest(project: Path, home_dir: Path) -> None:
    child = project / "src" / "pkg"
    child.mkdir(parents=True)
    found = find_state_root(child, home_dir)
    assert found is not None
    assert found.root == project.resolve()
    assert found.scope == "src/pkg"
    assert find_state_root(project, home_dir) == type(found)(project.resolve(), ".")


def test_find_state_root_treats_invalid_manifests_as_absent(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed"
    (malformed / ".deepsearch-axi").mkdir(parents=True)
    (malformed / ".deepsearch-axi" / "manifest.json").write_text("{not json", encoding="utf-8")
    assert find_state_root(malformed, tmp_path / "home") is None

    symlinked = tmp_path / "symlinked"
    real_manifest = install_manifest(tmp_path / "real")
    tool_dir = symlinked / ".deepsearch-axi"
    tool_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").symlink_to(real_manifest / ".deepsearch-axi" / "manifest.json")
    assert find_state_root(symlinked, tmp_path / "home") is None


def test_find_state_root_stops_after_home(tmp_path: Path) -> None:
    home = tmp_path / "fake-home"
    home.mkdir()
    cwd = home / "work"
    cwd.mkdir()
    install_manifest(tmp_path)
    assert find_state_root(cwd, home) is None
    install_manifest(home)
    found = find_state_root(cwd, home)
    assert found is not None
    assert found.root == home.resolve()
    assert found.scope == "work"


def test_find_state_root_caps_the_walk_at_64_levels(tmp_path: Path) -> None:
    shallow = tmp_path / "shallow"
    shallow.mkdir()
    current = shallow
    for _ in range(63):
        current = current / "d"
        current.mkdir()
    install_manifest(shallow)
    found = find_state_root(current, tmp_path / "home")
    assert found is not None and found.root == shallow.resolve()

    deep = tmp_path / "deep"
    deep.mkdir()
    current = deep
    for _ in range(64):
        current = current / "d"
        current.mkdir()
    install_manifest(deep)
    assert find_state_root(current, tmp_path / "home") is None


def test_record_session_event_requires_a_valid_manifest(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert record_session_event(plain, _event("s1"), "codex") is False
    assert not (plain / ".deepsearch-axi").exists()

    broken = tmp_path / "broken"
    install_manifest(broken)
    (broken / ".deepsearch-axi" / "manifest.json").write_text("{", encoding="utf-8")
    assert record_session_event(broken, _event("s1"), "codex") is False
    assert not _state_path(broken).exists()


def test_record_session_event_is_idempotent_and_updates_in_place(project: Path) -> None:
    start = _event("session-1", "start")
    assert record_session_event(project, start, "codex") is True
    before = _state_path(project).read_bytes()
    assert record_session_event(project, start, "codex") is False
    assert _state_path(project).read_bytes() == before

    end = _event("session-1", "end")
    assert record_session_event(project, end, "codex") is True
    state = _load_state(project)
    assert len(state["sessions"]) == 1
    assert state["sessions"][0] == {
        "id": start.session_hash,
        "agent": "codex",
        "scope": ".",
        "event": "end",
        "seq": 2,
    }
    assert state["seq"] == 2


def test_state_persists_only_the_hashed_session_identity(project: Path) -> None:
    raw = "raw-private-session-identifier"
    assert record_session_event(project, _event(raw), "codex") is True
    persisted = _state_path(project).read_text(encoding="utf-8")
    assert raw not in persisted
    assert session_hash("codex", raw) in persisted


def test_state_keeps_twenty_most_recent_session_records(project: Path) -> None:
    hashes = []
    for index in range(25):
        event = _event(f"session-{index}")
        hashes.append(event.session_hash)
        assert record_session_event(project, event, "codex") is True
    state = _load_state(project)
    assert len(state["sessions"]) == MAX_SESSION_RECORDS == 20
    assert [record["id"] for record in state["sessions"]] == hashes[-20:]
    assert [record["seq"] for record in state["sessions"]] == list(range(6, 26))
    assert state["seq"] == 25
    assert _state_path(project).stat().st_size <= MAX_STATE_FILE_BYTES


def test_state_size_eviction_keeps_the_file_within_the_limit(project: Path) -> None:
    huge_scope = "s" * 4000
    seeded = [
        {
            "id": session_hash("codex", f"old-{index}"),
            "agent": "codex",
            "scope": huge_scope,
            "event": "start",
            "seq": index + 1,
        }
        for index in range(MAX_SESSION_RECORDS)
    ]
    _seed_state(
        project,
        {"schema": 1, "seq": MAX_SESSION_RECORDS, "sessions": seeded, "scopes": {}},
    )
    newest = _event("newest")
    assert record_session_event(project, newest, "codex") is True
    raw = _state_path(project).read_bytes()
    assert len(raw) <= MAX_STATE_FILE_BYTES
    state = json.loads(raw)
    assert state["sessions"][-1]["id"] == newest.session_hash
    assert len(state["sessions"]) < MAX_SESSION_RECORDS


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        b"[]",
        b'{"schema":1,"seq":"zero","sessions":[],"scopes":{}}',
        b'{"schema":1,"seq":0,"sessions":[{"id":"short"}],"scopes":{}}',
        b"x" * 100_000,
    ],
)
def test_invalid_state_is_replaced_on_the_next_write(project: Path, payload: bytes) -> None:
    _state_path(project).write_bytes(payload)
    event = _event("after-corruption")
    assert record_session_event(project, event, "codex") is True
    state = _load_state(project)
    assert state["schema"] == 1
    assert state["seq"] == 1
    assert state["sessions"] == [
        {
            "id": event.session_hash,
            "agent": "codex",
            "scope": ".",
            "event": "start",
            "seq": 1,
        }
    ]


def test_record_research_tracks_exact_workspace_counters(project: Path) -> None:
    record_research(project, ".", "search", True)
    record_research(project, ".", "search", True)
    record_research(project, ".", "search", False)
    record_research(project, ".", "fetch", True)
    record_research(project, "sub", "fetch", False)
    _, activity = read_view(project, ".")
    assert activity == Activity(search_ok=2, search_fail=1, fetch_ok=1, fetch_fail=0)
    _, sibling = read_view(project, "sub")
    assert sibling == Activity(search_ok=0, search_fail=0, fetch_ok=0, fetch_fail=1)
    _, absent = read_view(project, "other")
    assert absent is None
    state = _load_state(project)
    assert state["seq"] == 5
    assert list(state["scopes"]) == [".", "sub"]


def test_record_research_keeps_twenty_scopes(project: Path) -> None:
    for index in range(25):
        record_research(project, f"scope-{index}", "search", True)
    state = _load_state(project)
    assert list(state["scopes"]) == [f"scope-{index}" for index in range(5, 25)]
    assert all(entry["search_ok"] == 1 for entry in state["scopes"].values())
    assert _state_path(project).stat().st_size <= MAX_STATE_FILE_BYTES


def test_record_research_without_a_manifest_creates_no_state(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    record_research(plain, ".", "search", True)
    assert not (plain / ".deepsearch-axi").exists()


def test_read_view_exact_scope_prior_and_exclusion(project: Path) -> None:
    first = _event("a", "end")
    sibling = _event("c", "end", scope="sibling")
    second = _event("b", "start")
    record_session_event(project, first, "codex")
    record_session_event(project, sibling, "codex")
    record_session_event(project, second, "codex")

    prior, activity = read_view(project, ".")
    assert prior == PriorEvent(agent="codex", event="start", sessions=2)
    assert activity is None

    excluded, _ = read_view(project, ".", exclude_session=second.session_hash)
    assert excluded == PriorEvent(agent="codex", event="end", sessions=2)

    sibling_prior, sibling_activity = read_view(project, "sibling")
    assert sibling_prior == PriorEvent(agent="codex", event="end", sessions=1)
    assert sibling_activity is None
    assert read_view(project, "sibling", exclude_session=sibling.session_hash) == (None, None)


def test_read_view_ignores_state_without_a_manifest(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    tool_dir = plain / ".deepsearch-axi"
    tool_dir.mkdir(parents=True)
    _seed_state(
        plain,
        {
            "schema": 1,
            "seq": 1,
            "sessions": [
                {
                    "id": session_hash("codex", "s"),
                    "agent": "codex",
                    "scope": ".",
                    "event": "start",
                    "seq": 1,
                }
            ],
            "scopes": {
                ".": {"search_ok": 1, "search_fail": 0, "fetch_ok": 0, "fetch_fail": 0, "seq": 1}
            },
        },
    )
    assert read_view(plain, ".") == (None, None)


def _research_worker(root: str, scope: str, tool: str, ok: bool, times: int) -> None:
    from pathlib import Path as WorkerPath

    from deepsearch_axi.state import record_research

    for _ in range(times):
        record_research(WorkerPath(root), scope, tool, ok)


def _session_worker(root: str, agent: str, start: int, times: int) -> None:
    from pathlib import Path as WorkerPath

    from deepsearch_axi.models import NativeEvent
    from deepsearch_axi.state import record_session_event, session_hash

    for index in range(start, start + times):
        event = NativeEvent(session_hash(agent, f"worker-{index}"), ".", "start")
        record_session_event(WorkerPath(root), event, agent)


def test_concurrent_research_events_keep_exact_counters(project: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_research_worker, args=(str(project), ".", "search", True, 25)),
        context.Process(target=_research_worker, args=(str(project), ".", "search", True, 25)),
        context.Process(target=_research_worker, args=(str(project), ".", "search", True, 25)),
        context.Process(target=_research_worker, args=(str(project), ".", "search", False, 25)),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    state = _load_state(project)
    assert state["scopes"]["."]["search_ok"] == 75
    assert state["scopes"]["."]["search_fail"] == 25
    assert state["seq"] == 100
    assert state["schema"] == 1


def test_concurrent_session_events_keep_exact_sequence(project: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_session_worker, args=(str(project), "codex", start, 4))
        for start in (0, 4, 8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
    assert [process.exitcode for process in processes] == [0, 0, 0]
    state = _load_state(project)
    assert state["seq"] == 12
    assert len(state["sessions"]) == 12
    assert len({record["id"] for record in state["sessions"]}) == 12
    assert sorted(record["seq"] for record in state["sessions"]) == list(range(1, 13))
    assert _state_path(project).stat().st_size <= MAX_STATE_FILE_BYTES


def test_symlinked_state_file_cannot_escape_the_project(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-state.json"
    outside.write_bytes(b'{"sentinel":"untouched"}')
    (project / STATE_RELATIVE).symlink_to(outside)
    event = _event("symlink-session")
    assert record_session_event(project, event, "codex") is True
    assert outside.read_bytes() == b'{"sentinel":"untouched"}'
    assert not (project / STATE_RELATIVE).is_symlink()
    assert _load_state(project)["sessions"][0]["id"] == event.session_hash


def test_symlinked_tool_directory_is_never_written(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-tool-dir"
    outside.mkdir()
    (project / ".deepsearch-axi").symlink_to(outside)
    manifest = Manifest(
        project=str(project.resolve()),
        executable="/usr/local/bin/deepsearch-axi",
        command=("deepsearch-axi",),
        hosts={},
    )
    (outside / "manifest.json").write_bytes(manifest_bytes(manifest))
    assert record_session_event(project, _event("s1"), "codex") is False
    record_research(project, ".", "search", True)
    assert not (outside / "state.json").exists()
    assert not (outside / "state.lock").exists()
