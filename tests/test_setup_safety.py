"""Setup safety edges documented in the README: refusal, rollback and resolution.

These tests cover the failure paths the coder's setup suite does not reach:
a symlinked host directory must refuse the whole multi-host run before any
write, a write that fails partway must restore every earlier destination, and
a process without a sibling console script must return the structured
``invalid_config`` failure instead of registering an unrunnable command.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from support.cli import decode, run_cli
from support.env import FAKE_KEY, isolated_env

from deepsearch_axi import fsutil, installer
from deepsearch_axi.executable import resolve_running_executable
from deepsearch_axi.models import DeepsearchError, ExecutableIdentity

CLAUDE = Path(".claude") / "settings.json"
CODEX_HOOKS = Path(".codex") / "hooks.json"
CODEX_CONFIG = Path(".codex") / "config.toml"
PLUGIN = Path(".opencode") / "plugins" / "deepsearch-axi.js"
MANIFEST = Path(".deepsearch-axi") / "manifest.json"


def _fake_console(home: Path) -> Path:
    script = home / "bin" / "deepsearch-axi"
    script.parent.mkdir(parents=True)
    script.write_text(
        f"#!{sys.executable}\nfrom deepsearch_axi._entry import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _env(home: Path) -> dict[str, str]:
    environment = isolated_env(home, DEEPSEEK_API_KEY=FAKE_KEY)
    environment["PATH"] = os.environ.get("PATH", "")
    return environment


def _tree(root: Path) -> list[tuple[str, str, bytes | None]]:
    """Return every entry with its kind and bytes; symlinks are recorded, not followed."""
    entries: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path).encode("utf-8")))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        else:
            entries.append((relative, "dir", None))
    return entries


def test_symlinked_host_directory_refuses_the_whole_run_without_writes(
    tmp_path: Path, home_dir: Path, project_dir: Path
) -> None:
    executable = _fake_console(home_dir)
    environment = _env(home_dir)
    outside = tmp_path / "outside-claude"
    outside.mkdir()
    (project_dir / ".claude").symlink_to(outside, target_is_directory=True)
    before = _tree(project_dir)

    run = run_cli(["setup"], env=environment, cwd=project_dir, executable=[str(executable)])

    assert run.code == 1, (run.stdout, run.stderr)
    assert run.stderr == ""
    error = decode(run.stdout)["error"]
    assert error["code"] == "invalid_config"
    assert "symlink or path escapes the project" in error["message"]
    # Codex and OpenCode plan successfully, but the claude-code failure must
    # abort the transaction before any destination is written.
    assert _tree(project_dir) == before
    assert list(outside.iterdir()) == []
    for relative in (CODEX_HOOKS, CODEX_CONFIG, PLUGIN, MANIFEST):
        assert not (project_dir / relative).exists(), relative


def test_partial_repair_reports_unchanged_hosts_and_leaves_them_byte_identical(
    home_dir: Path, project_dir: Path
) -> None:
    executable = _fake_console(home_dir)
    environment = _env(home_dir)
    first = run_cli(["setup"], env=environment, cwd=project_dir, executable=[str(executable)])
    assert first.code == 0, (first.stdout, first.stderr)

    plugin = project_dir / PLUGIN
    expected_plugin = plugin.read_bytes()
    untouched = {
        relative: (
            (project_dir / relative).read_bytes(),
            (project_dir / relative).stat().st_mtime_ns,
        )
        for relative in (CLAUDE, CODEX_HOOKS, CODEX_CONFIG)
    }
    plugin.unlink()

    repaired = run_cli(["setup"], env=environment, cwd=project_dir, executable=[str(executable)])

    assert repaired.code == 0, (repaired.stdout, repaired.stderr)
    assert repaired.stderr == ""
    rows = {row["agent"]: row["status"] for row in decode(repaired.stdout)["hosts"]}
    assert rows == {"claude-code": "unchanged", "codex": "unchanged", "opencode": "repaired"}
    assert plugin.read_bytes() == expected_plugin
    for relative, (data, mtime) in untouched.items():
        path = project_dir / relative
        assert path.read_bytes() == data, relative
        assert path.stat().st_mtime_ns == mtime, relative

    home = run_cli([], env=environment, cwd=project_dir, executable=[str(executable)])
    assert home.code == 0, (home.stdout, home.stderr)
    assert decode(home.stdout)["integrations"] == {
        "claude-code": "installed",
        "codex": "installed",
        "opencode": "installed",
    }


def test_failed_write_restores_every_earlier_destination(
    project_dir: Path,
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_console(home_dir)
    original_claude = b'{"model": "opus"}\n'
    (project_dir / ".claude").mkdir()
    (project_dir / CLAUDE).write_bytes(original_claude)
    before = _tree(project_dir)

    real_atomic_write = fsutil.atomic_write
    attempted: list[Path] = []

    def failing_plugin_write(path: Path, data: bytes) -> None:
        attempted.append(path)
        # Claude, Codex hooks and Codex config are already written when the
        # OpenCode plugin write fails; every one of them must be rolled back.
        if path.name == "deepsearch-axi.js":
            raise OSError("simulated write failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(fsutil, "atomic_write", failing_plugin_write)
    identity = ExecutableIdentity(path=executable, command=(str(executable),))

    with pytest.raises(DeepsearchError) as info:
        installer.setup_project(project_dir, "all", identity)

    assert info.value.failure.code == "local_io"
    assert any(path.name == "deepsearch-axi.js" for path in attempted)
    assert (project_dir / CLAUDE).read_bytes() == original_claude
    for relative in (CODEX_HOOKS, CODEX_CONFIG, PLUGIN, MANIFEST):
        assert not (project_dir / relative).exists(), relative
    after = _tree(project_dir)
    # Only the tool lock directory may remain: it is not a host destination.
    assert [entry for entry in after if not entry[0].startswith(".deepsearch-axi")] == [
        entry for entry in before if not entry[0].startswith(".deepsearch-axi")
    ]


def test_resolution_without_a_sibling_console_script_is_invalid_config(tmp_path: Path) -> None:
    interpreter_dir = tmp_path / "bin"
    interpreter_dir.mkdir()
    fake_python = interpreter_dir / "python"
    fake_python.write_text("", encoding="utf-8")

    with pytest.raises(DeepsearchError) as info:
        resolve_running_executable(
            argv0=str(tmp_path / "deepsearch_axi" / "__main__.py"),
            python_executable=str(fake_python),
        )
    assert info.value.failure.code == "invalid_config"
    assert info.value.failure.message == (
        "The deepsearch-axi console script could not be resolved."
    )


def test_resolution_prefers_a_sibling_console_script_for_module_invocation(
    tmp_path: Path,
) -> None:
    interpreter_dir = tmp_path / "bin"
    interpreter_dir.mkdir()
    fake_python = interpreter_dir / "python"
    fake_python.write_text("", encoding="utf-8")
    sibling = interpreter_dir / "deepsearch-axi"
    sibling.write_text("#!/bin/sh\n", encoding="utf-8")
    sibling.chmod(0o755)

    resolved = resolve_running_executable(
        argv0=str(tmp_path / "deepsearch_axi" / "__main__.py"),
        python_executable=str(fake_python),
    )
    assert resolved == sibling.absolute()
