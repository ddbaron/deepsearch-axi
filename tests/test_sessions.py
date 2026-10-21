"""AC-32: native payload, scope and state-privacy behavior through the real CLI.

The opt-in fixtures below write a valid tool-owned manifest directly.  They are
honest fixtures for this gate only: they do not claim to satisfy generated
registration AC-30 or the setup gates AC-25--AC-29, which exercise host files
and setup transactions separately.
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
from pathlib import Path

from support.cli import decode, run_cli
from support.env import FAKE_KEY, isolated_env
from support.stub_server import json_response

from deepsearch_axi.manifest import Manifest, manifest_bytes
from deepsearch_axi.state import STATE_RELATIVE, session_hash


def _opt_in(project: Path) -> Path:
    """Write a valid tool-owned manifest without running setup registration."""
    owned = project / ".deepsearch-axi"
    owned.mkdir()
    manifest = Manifest(
        project=str(project.resolve()),
        executable="/usr/local/bin/deepsearch-axi",
        command=("deepsearch-axi",),
        hosts={},
    )
    (owned / "manifest.json").write_bytes(manifest_bytes(manifest))
    return owned


def test_payload_scope_privacy_and_idempotence(tmp_path: Path, home_dir: Path, stub) -> None:
    project = tmp_path / "project"
    project.mkdir()
    owned = _opt_in(project)
    state_path = project / STATE_RELATIVE
    env = isolated_env(home_dir)

    def run_session(event: str, payload: bytes):
        return run_cli(
            ["session", event, "--agent", "codex", "--project", str(project)],
            env=env,
            cwd=project,
            stdin=payload,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("SECRET-TRANSCRIPT-CONTENT", encoding="utf-8")
    transcript.chmod(0)
    symlink = project / "escape"
    symlink.symlink_to(outside, target_is_directory=True)

    invalid_payloads = [
        ("not-json", b"not json"),
        ("array-root", b"[]"),
        ("missing-session", json.dumps({"cwd": str(project)}).encode("utf-8")),
        ("missing-cwd", json.dumps({"session_id": "session-1"}).encode("utf-8")),
        (
            "traversal-id",
            json.dumps({"session_id": "../escape", "cwd": str(project)}).encode("utf-8"),
        ),
        (
            "nested-id",
            json.dumps({"session_id": "a/b", "cwd": str(project)}).encode("utf-8"),
        ),
        (
            "backslash-id",
            json.dumps({"session_id": "a\\b", "cwd": str(project)}).encode("utf-8"),
        ),
        (
            "nul-id",
            json.dumps({"session_id": "a\u0000b", "cwd": str(project)}).encode("utf-8"),
        ),
        (
            "too-long-id",
            json.dumps({"session_id": "x" * 513, "cwd": str(project)}).encode("utf-8"),
        ),
        (
            "relative-cwd",
            json.dumps({"session_id": "session-1", "cwd": "relative"}).encode("utf-8"),
        ),
        (
            "outside-cwd",
            json.dumps({"session_id": "session-1", "cwd": str(outside)}).encode("utf-8"),
        ),
        (
            "symlink-cwd",
            json.dumps({"session_id": "session-1", "cwd": str(symlink)}).encode("utf-8"),
        ),
    ]
    for label, payload in invalid_payloads:
        run = run_session("start", payload)
        assert run.code == 1, label
        assert run.stderr == "", label
        document = decode(run.stdout)
        assert document["error"]["code"] == "invalid_event", label
        assert "SECRET" not in run.stdout + run.stderr, label
    assert not state_path.exists()

    oversized = (
        b'{"session_id":"session-1","cwd":"'
        + str(project).encode("utf-8")
        + b'","padding":"'
        + b"x" * 70000
        + b'"}'
    )
    run = run_session("start", oversized)
    assert run.code == 1
    assert decode(run.stdout)["error"]["code"] == "invalid_event"
    assert "payload exceeds" in decode(run.stdout)["error"]["message"]
    assert not state_path.exists()

    # A real TTY reaches the same bounded reader and is refused before any read.
    master, slave = pty.openpty()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "deepsearch_axi",
                "session",
                "start",
                "--agent",
                "codex",
                "--project",
                str(project),
            ],
            cwd=str(project),
            env=env,
            stdin=slave,
            capture_output=True,
            timeout=60,
        )
    finally:
        os.close(slave)
        os.close(master)
    assert completed.returncode == 1
    assert completed.stderr == b""
    tty_document = decode(completed.stdout.decode("utf-8"))
    assert tty_document["error"]["code"] == "invalid_event"
    assert "stdin is not piped" in tty_document["error"]["message"]
    assert not state_path.exists()

    # Missing-key start succeeds and records only the hashed event.
    private_payload = {
        "session_id": "private-session-0001",
        "cwd": str(project),
        "transcript_path": str(transcript),
        "hook_event_name": "SessionStart",
        "query": "PRIVATE-QUERY",
        "content": "PRIVATE-CONTENT",
        "api_key": "sk-PRIVATE-KEY",
        "prompt": "PRIVATE-PROMPT",
        "nested": {"secret": "PRIVATE-NESTED"},
    }
    run = run_session("start", json.dumps(private_payload).encode("utf-8"))
    assert run.code == 0, run.stdout + run.stderr
    document = decode(run.stdout)
    assert document["auth"]["state"] == "missing"
    assert document["cwd"] == str(project)
    assert run.stderr == ""

    persisted = state_path.read_text(encoding="utf-8")
    for private in (
        "private-session-0001",
        "SECRET-TRANSCRIPT-CONTENT",
        "transcript.jsonl",
        "PRIVATE-QUERY",
        "PRIVATE-CONTENT",
        "sk-PRIVATE-KEY",
        "PRIVATE-PROMPT",
        "PRIVATE-NESTED",
    ):
        assert private not in persisted, private
    assert session_hash("codex", "private-session-0001") in persisted
    state = json.loads(persisted)
    assert state["sessions"] == [
        {
            "id": session_hash("codex", "private-session-0001"),
            "agent": "codex",
            "scope": ".",
            "event": "start",
            "seq": 1,
        }
    ]
    assert sentinel.read_text(encoding="utf-8") == "untouched"

    # Repeating the same end event is a byte-identical no-op.
    end_payload = json.dumps({"session_id": "end-session-0001", "cwd": str(project)}).encode(
        "utf-8"
    )
    first_end = run_session("end", end_payload)
    assert first_end.code == 0, first_end.stdout + first_end.stderr
    assert decode(first_end.stdout) == {"session": {"event": "end", "recorded": True}}
    before = state_path.read_bytes()
    second_end = run_session("end", end_payload)
    assert second_end.code == 0
    assert decode(second_end.stdout) == {"session": {"event": "end", "recorded": False}}
    assert state_path.read_bytes() == before

    # Sibling session and research activity is recorded but never disclosed in
    # another cwd's ambient context (and the counters remain exact).
    sibling = project / "sibling"
    sibling.mkdir()
    sibling_run = run_session(
        "start",
        json.dumps({"session_id": "sibling-session", "cwd": str(sibling)}).encode("utf-8"),
    )
    assert sibling_run.code == 0, sibling_run.stdout + sibling_run.stderr
    search_server = stub(lambda request: json_response({"results": []}))
    search_env = isolated_env(
        home_dir,
        DEEPSEEK_API_KEY=FAKE_KEY,
        DEEPSEEK_BASE_URL=search_server.base_url,
    )
    search_run = run_cli(["search", "sibling probe"], env=search_env, cwd=sibling)
    assert search_run.code == 0, search_run.stdout + search_run.stderr
    assert len(search_server.requests) == 1

    fresh = project / "fresh"
    fresh.mkdir()
    fresh_run = run_session(
        "start",
        json.dumps({"session_id": "fresh-session", "cwd": str(fresh)}).encode("utf-8"),
    )
    assert fresh_run.code == 0, fresh_run.stdout + fresh_run.stderr
    fresh_document = decode(fresh_run.stdout)
    assert "prior" not in fresh_document
    assert "activity" not in fresh_document
    assert session_hash("codex", "sibling-session") not in fresh_run.stdout

    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    sibling_counters = final_state["scopes"]["sibling"]
    assert sibling_counters["search_ok"] == 1
    assert sibling_counters["search_fail"] == 0
    assert sibling_counters["fetch_ok"] == 0
    assert sibling_counters["fetch_fail"] == 0
    assert "fresh" not in final_state["scopes"]
    assert (owned / "manifest.json").is_file()
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel.txt"]
    assert sentinel.read_text(encoding="utf-8") == "untouched"
