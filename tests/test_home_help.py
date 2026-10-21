"""AC-22/23: executable home readiness and spec-driven command help.

The home view is exercised through a real console-script child process so its
``bin`` identity, live integration inspection, scoped prior activity and size
budgets match installed behavior. Help is exercised for every implemented
command path with an audit hook proving no credential/config-file reads.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from support.cli import decode, run_cli
from support.env import FAKE_KEY, REPO_ROOT
from support.stub_server import StubServer, json_response

from deepsearch_axi._version import VERSION
from deepsearch_axi.guidance import (
    DESCRIPTION,
    HINT_FETCH,
    HINT_SEARCH,
    HINT_SET_KEY,
    HINT_SETUP,
)
from deepsearch_axi.manifest import Manifest, ManifestHost, manifest_bytes
from deepsearch_axi.models import OFFICIAL_ORIGIN

USER_DOTENV = Path(".config") / "deepsearch-axi" / ".env"
MANIFEST_RELATIVE = Path(".deepsearch-axi") / "manifest.json"
PLUGIN_TEMPLATE = REPO_ROOT / "src" / "deepsearch_axi" / "templates" / "opencode_plugin.js"


def _fake_console(home: Path) -> Path:
    """Create a real executable console script inside the isolated HOME."""
    script = home / "bin" / "deepsearch-axi"
    script.parent.mkdir(parents=True)
    script.write_text(
        f"#!{sys.executable}\nfrom deepsearch_axi._entry import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _hook_document(start: str, end: str) -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": start}]},
                {"matcher": "startup", "hooks": [{"type": "command", "command": "echo unrelated"}]},
            ],
            "SessionEnd": [
                {"hooks": [{"type": "command", "command": end}]},
                {"hooks": [{"type": "command", "command": "echo unrelated"}]},
            ],
        }
    }


def _lifecycle_commands(prefix: Sequence[str], project: Path, agent: str) -> tuple[str, str]:
    root = str(project.resolve())
    start = shlex.join([*prefix, "session", "start", "--agent", agent, "--project", root])
    end = shlex.join([*prefix, "session", "end", "--agent", agent, "--project", root])
    return start, end


def _plugin_text(prefix: Sequence[str], project: Path) -> str:
    text = PLUGIN_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("__DEEPSEARCH_AXI_COMMAND__", json.dumps(list(prefix)))
    text = text.replace("__DEEPSEARCH_AXI_PROJECT__", json.dumps(str(project.resolve())))
    return text.replace("__DEEPSEARCH_AXI_VERSION__", json.dumps(VERSION))


def _write_manifest(
    project: Path,
    executable: Path,
    *,
    recorded_executable: str | None = None,
    command: Sequence[str] | None = None,
) -> Manifest:
    prefix = [str(executable)] if command is None else list(command)
    hosts = {
        "claude-code": ManifestHost(
            files=(".claude/settings.json",),
            commands=_lifecycle_commands(prefix, project, "claude-code"),
        ),
        "codex": ManifestHost(
            files=(".codex/hooks.json", ".codex/config.toml"),
            commands=_lifecycle_commands(prefix, project, "codex"),
        ),
        "opencode": ManifestHost(
            files=(".opencode/plugins/deepsearch-axi.js",),
            commands=(json.dumps(prefix),),
        ),
    }
    manifest = Manifest(
        project=str(project.resolve()),
        executable=recorded_executable or str(executable),
        command=tuple(prefix),
        hosts=hosts,
    )
    owned = project / ".deepsearch-axi"
    owned.mkdir(parents=True, exist_ok=True)
    (owned / "manifest.json").write_bytes(manifest_bytes(manifest))
    return manifest


def _write_setup(
    project: Path,
    executable: Path,
    *,
    corrupt: str | None = None,
    prefix: Sequence[str] | None = None,
) -> None:
    """Write a native integration fixture, optionally corrupting one surface."""
    project.mkdir(parents=True, exist_ok=True)
    command_prefix = [str(executable)] if prefix is None else list(prefix)
    codex_commands = _lifecycle_commands(command_prefix, project, "codex")

    claude_path = project / ".claude" / "settings.json"
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    # A foreign-project fixture points every owned Claude command at a sibling root.
    claude_root = (
        project.parent / "sibling-project" if corrupt == "claude-foreign-project" else project
    )
    claude_commands = _lifecycle_commands(command_prefix, claude_root, "claude-code")
    claude_document = _hook_document(*claude_commands)
    if corrupt == "claude-missing":
        del claude_document["hooks"]["SessionEnd"]
    elif corrupt == "claude-altered":
        claude_document["hooks"]["SessionEnd"][0]["hooks"][0]["command"] += " --debug"
    elif corrupt == "claude-untyped":
        del claude_document["hooks"]["SessionStart"][0]["hooks"][0]["type"]
    elif corrupt == "claude-duplicate":
        claude_document["hooks"]["SessionStart"].append(
            {"hooks": [{"type": "command", "command": claude_commands[0]}]}
        )
    elif corrupt == "claude-outdated":
        outdated = shlex.join(
            [
                "/opt/old/deepsearch-axi",
                "session",
                "end",
                "--agent",
                "claude-code",
                "--project",
                str(project.resolve()),
            ]
        )
        claude_document["hooks"]["SessionEnd"].append(
            {"hooks": [{"type": "command", "command": outdated}]}
        )
    elif corrupt == "claude-prompt":
        claude_document["hooks"]["SessionStart"][0]["hooks"][0]["type"] = "prompt"
    elif corrupt == "claude-disable-all":
        claude_document["disableAllHooks"] = True
    elif corrupt == "claude-matcher-never":
        claude_document["hooks"]["SessionStart"][0]["matcher"] = "^never-a-session-source$"
    elif corrupt == "claude-matcher-partial":
        claude_document["hooks"]["SessionStart"][0]["matcher"] = "startup"
    elif corrupt == "claude-matcher-untyped":
        claude_document["hooks"]["SessionStart"][0]["matcher"] = 123
    elif corrupt == "claude-matcher-invalid":
        claude_document["hooks"]["SessionStart"][0]["matcher"] = "["
    elif corrupt == "claude-matcher-valid":
        claude_document["hooks"]["SessionStart"][0]["matcher"] = "startup|resume|clear|compact"
    elif corrupt == "claude-matcher-unrelated-bad":
        # Both bad matchers sit on unrelated groups, which must stay ignored.
        claude_document["hooks"]["SessionStart"][1]["matcher"] = 123
        claude_document["hooks"]["SessionEnd"][1]["matcher"] = "^never-a-session-source$"
    claude_path.write_text(json.dumps(claude_document, indent=2) + "\n", encoding="utf-8")

    codex_path = project / ".codex" / "hooks.json"
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_document = _hook_document(*codex_commands)
    if corrupt == "codex-missing":
        del codex_document["hooks"]["SessionEnd"]
    elif corrupt == "codex-altered":
        codex_document["hooks"]["SessionEnd"][0]["hooks"][0]["command"] += " --debug"
    elif corrupt == "codex-duplicate":
        codex_document["hooks"]["SessionStart"].append(
            {"hooks": [{"type": "command", "command": codex_commands[0]}]}
        )
    if corrupt == "codex-json":
        codex_path.write_text("{not valid json\n", encoding="utf-8")
    else:
        codex_path.write_text(json.dumps(codex_document, indent=2) + "\n", encoding="utf-8")

    config_path = project / ".codex" / "config.toml"
    if corrupt == "codex-features":
        config_path.write_text("[features]\nhooks = false\n", encoding="utf-8")
    elif corrupt == "codex-features-absent":
        config_path.write_text("[other]\nvalue = 1\n", encoding="utf-8")
    else:
        config_path.write_text("[features]\nhooks = true\n", encoding="utf-8")

    plugin_path = project / ".opencode" / "plugins" / "deepsearch-axi.js"
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    plugin = _plugin_text(command_prefix, project)
    if corrupt == "plugin-unmanaged":
        plugin = "// local hand-written plugin\n" + plugin.split("\n", 1)[1]
    elif corrupt == "plugin-mismatch":
        plugin = plugin.replace(json.dumps(command_prefix), '["other-command"]', 1)
    plugin_path.write_text(plugin, encoding="utf-8")

    _write_manifest(
        project,
        executable,
        command=command_prefix,
        recorded_executable=(
            "/nonexistent/deepsearch-axi" if corrupt == "stale-executable" else None
        ),
    )
    # Missing managed files keep a plausible manifest but must read as stale.
    removals = {
        "claude-missing-file": claude_path,
        "codex-hooks-missing-file": codex_path,
        "codex-config-missing": config_path,
        "plugin-missing-file": plugin_path,
    }
    if corrupt in removals:
        removals[corrupt].unlink()


def _snapshot(root: Path) -> dict[str, tuple[bytes, int] | None]:
    snapshot: dict[str, tuple[bytes, int] | None] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = (b"<symlink>", 0)
        elif path.is_file():
            snapshot[relative] = (path.read_bytes(), path.stat().st_mtime_ns)
        else:
            snapshot[relative + "/"] = None
    return snapshot


def _home(
    env: dict[str, str],
    cwd: Path,
    executable: Path,
    *,
    read_guard: list[Path] | None = None,
):
    run = run_cli([], env=env, cwd=cwd, executable=[str(executable)], read_guard=read_guard or ())
    assert run.code == 0, (run.stdout, run.stderr)
    assert run.stderr == ""
    return run, decode(run.stdout)


def test_home_readiness(
    stub: Callable[..., StubServer],
    env: dict[str, str],
    home_dir: Path,
    project_dir: Path,
    tmp_path: Path,
) -> None:
    server = stub(lambda request: json_response({"unexpected": True}))
    executable = _fake_console(home_dir)
    base_env = dict(env, DEEPSEEK_BASE_URL=server.base_url)

    # Missing credential, no manifest, live cwd and no registration.
    before_project = _snapshot(project_dir)
    before_home = _snapshot(home_dir)
    run, document = _home(base_env, project_dir, executable)
    assert document["bin"] == "~/bin/deepsearch-axi"
    assert document["description"] == DESCRIPTION
    assert document["cwd"] == str(project_dir.resolve())
    assert document["auth"] == {"state": "missing", "source": "none", "verified": False}
    assert document["api"] == {"origin": server.base_url, "state": "ready"}
    assert document["integrations"] == {
        "claude-code": "missing",
        "codex": "missing",
        "opencode": "missing",
    }
    assert document["help"] == [HINT_SET_KEY, HINT_SETUP]
    assert len(run.stdout) <= 1800
    assert FAKE_KEY not in run.stdout + run.stderr
    assert server.requests == []
    assert _snapshot(project_dir) == before_project
    assert _snapshot(home_dir) == before_home

    # Configured through the environment; readiness stays explicitly unverified.
    _, configured = _home(dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), project_dir, executable)
    assert configured["auth"] == {"state": "configured", "source": "env", "verified": False}
    assert configured["help"] == [HINT_SEARCH, HINT_SETUP]
    assert FAKE_KEY not in json.dumps(configured)

    _, invalid_env = _home(dict(base_env, DEEPSEEK_API_KEY="bad key"), project_dir, executable)
    assert invalid_env["auth"] == {"state": "invalid", "source": "env", "verified": False}

    # Configured through the exact-cwd dotenv, then an invalid dotenv source.
    (project_dir / ".env").write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    _, dotenv = _home(base_env, project_dir, executable)
    assert dotenv["auth"] == {"state": "configured", "source": "cwd_dotenv", "verified": False}
    (project_dir / ".env").write_text('DEEPSEEK_API_KEY="unterminated\n', encoding="utf-8")
    _, invalid_key = _home(base_env, project_dir, executable)
    assert invalid_key["auth"] == {
        "state": "invalid",
        "source": "cwd_dotenv",
        "verified": False,
    }
    assert "fix DEEPSEEK_API_KEY at its source" in invalid_key["help"][0]
    (project_dir / ".env").unlink()

    # The user-level dotenv is the lowest-precedence file source.
    user_dotenv = home_dir / USER_DOTENV
    user_dotenv.parent.mkdir(parents=True)
    user_dotenv.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    _, user_source = _home(base_env, project_dir, executable)
    assert user_source["auth"] == {
        "state": "configured",
        "source": "user_dotenv",
        "verified": False,
    }
    user_dotenv.unlink()

    # The default origin is the official API, without a probe.
    _, official = _home(dict(env, DEEPSEEK_API_KEY=FAKE_KEY), project_dir, executable)
    assert official["api"] == {"origin": OFFICIAL_ORIGIN, "state": "ready"}

    # Invalid origin is reported without echoing the unsafe value.
    bad_origin = "http://example.com/api"
    _, invalid_origin = _home(
        dict(env, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=bad_origin),
        project_dir,
        executable,
    )
    assert invalid_origin["api"] == {"origin": "invalid", "state": "invalid"}
    assert invalid_origin["help"][0] == "unset DEEPSEEK_BASE_URL or set an https origin"
    assert bad_origin not in json.dumps(invalid_origin)

    # Installed integrations: live files must back every manifest record.
    installed_project = tmp_path / "installed"
    _write_setup(installed_project, executable)
    # The byte budgets below are only meaningful for the standard bounded-path
    # fixture, so the fixture's own path length is asserted explicitly.
    assert len(str(installed_project.resolve())) <= 160
    installed_run, installed = _home(
        dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), installed_project, executable
    )
    assert installed["integrations"] == {
        "claude-code": "installed",
        "codex": "installed",
        "opencode": "installed",
    }
    assert installed["help"] == [HINT_SEARCH, HINT_FETCH]
    assert len(installed_run.stdout) <= 1800
    assert server.requests == []

    # Every damaged or unmanaged surface is stale even with a plausible
    # manifest; untouched siblings stay installed.
    stale_cases = (
        ("codex-json", "codex"),
        ("codex-features", "codex"),
        ("codex-features-absent", "codex"),
        ("codex-missing", "codex"),
        ("codex-altered", "codex"),
        ("codex-duplicate", "codex"),
        ("codex-hooks-missing-file", "codex"),
        ("codex-config-missing", "codex"),
        ("claude-missing", "claude-code"),
        ("claude-altered", "claude-code"),
        ("claude-missing-file", "claude-code"),
        ("claude-duplicate", "claude-code"),
        ("claude-outdated", "claude-code"),
        ("claude-prompt", "claude-code"),
        ("claude-untyped", "claude-code"),
        ("claude-disable-all", "claude-code"),
        ("claude-foreign-project", "claude-code"),
        # The owned group's matcher must still cover every SessionStart source.
        ("claude-matcher-never", "claude-code"),
        ("claude-matcher-partial", "claude-code"),
        ("claude-matcher-untyped", "claude-code"),
        ("claude-matcher-invalid", "claude-code"),
        ("plugin-unmanaged", "opencode"),
        ("plugin-mismatch", "opencode"),
        ("plugin-missing-file", "opencode"),
    )
    for corrupt, expected_stale in stale_cases:
        project = tmp_path / f"stale-{corrupt}"
        _write_setup(project, executable, corrupt=corrupt)
        _, stale = _home(dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), project, executable)
        assert stale["integrations"][expected_stale] == "stale", (corrupt, stale["integrations"])
        for agent, state in stale["integrations"].items():
            if agent != expected_stale:
                assert state == "installed", (corrupt, agent, stale["integrations"])

    # A valid owned matcher still captures every start, and malformed matchers
    # on unrelated groups can never change the owned-group result.
    for corrupt in ("claude-matcher-valid", "claude-matcher-unrelated-bad"):
        project = tmp_path / f"ready-{corrupt}"
        _write_setup(project, executable, corrupt=corrupt)
        _, ready = _home(dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), project, executable)
        assert ready["integrations"] == {
            "claude-code": "installed",
            "codex": "installed",
            "opencode": "installed",
        }, (corrupt, ready["integrations"])

    identity_project = tmp_path / "stale-identity"
    _write_setup(identity_project, executable, corrupt="stale-executable")
    _, identity = _home(dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), identity_project, executable)
    assert identity["integrations"] == {
        "claude-code": "stale",
        "codex": "stale",
        "opencode": "stale",
    }

    # Scoped prior activity: the exact cwd scope is disclosed, siblings are not.
    state = {
        "schema": 1,
        "seq": 4,
        "sessions": [
            {
                "id": "a" * 32,
                "agent": "codex",
                "scope": ".",
                "event": "start",
                "seq": 1,
            }
        ],
        "scopes": {
            ".": {"search_ok": 2, "search_fail": 1, "fetch_ok": 1, "fetch_fail": 0, "seq": 4}
        },
    }
    owned = installed_project / ".deepsearch-axi"
    (owned / "state.json").write_text(json.dumps(state), encoding="utf-8")
    prior_run, with_prior = _home(
        dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), installed_project, executable
    )
    assert with_prior["prior"] == {"agent": "codex", "event": "start", "sessions": 1}
    assert with_prior["activity"] == {
        "search_ok": 2,
        "search_fail": 1,
        "fetch_ok": 1,
        "fetch_fail": 0,
    }
    assert len(prior_run.stdout) <= 1800

    child = installed_project / "child"
    child.mkdir()
    _, child_view = _home(dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), child, executable)
    assert "prior" not in child_view
    assert "activity" not in child_view
    assert child_view["cwd"] == str(child.resolve())
    assert child_view["integrations"] == installed["integrations"]

    # Ambient session-start context stays inside its tighter budget.
    payload = json.dumps({"session_id": "ambient-1", "cwd": str(installed_project)}).encode()
    ambient = run_cli(
        ["session", "start", "--agent", "codex", "--project", str(installed_project)],
        env=dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY),
        cwd=installed_project,
        executable=[str(executable)],
        stdin=payload,
    )
    assert ambient.code == 0, ambient.stderr
    assert ambient.stderr == ""
    ambient_document = decode(ambient.stdout)
    assert ambient_document["integration"] == "installed"
    assert ambient_document["bin"] == "~/bin/deepsearch-axi"
    assert ambient_document["cwd"] == str(installed_project.resolve())
    assert ambient_document["description"] == DESCRIPTION
    assert len(ambient.stdout) <= 1200
    assert FAKE_KEY not in ambient.stdout + ambient.stderr

    after_session_run, after_session = _home(
        dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), installed_project, executable
    )
    assert after_session["prior"] == {"agent": "codex", "event": "start", "sessions": 2}
    assert len(after_session_run.stdout) <= 1800
    assert server.requests == []


def test_home_rechecks_manifest_command(
    env: dict[str, str], home_dir: Path, tmp_path: Path
) -> None:
    """A recorded command prefix must resolve to the live console script.

    The manifest owns the hook and plugin command strings, so it cannot vouch
    for itself: the effective prefix is re-resolved through the filesystem or
    PATH and compared with the running executable by file identity.
    """
    executable = _fake_console(home_dir)
    base_env = dict(env, DEEPSEEK_API_KEY=FAKE_KEY)

    missing_project = tmp_path / "command-missing"
    _write_setup(missing_project, executable, prefix=("/nonexistent/deepsearch-axi",))
    _, missing = _home(base_env, missing_project, executable)
    assert missing["integrations"] == {
        "claude-code": "stale",
        "codex": "stale",
        "opencode": "stale",
    }

    bare_project = tmp_path / "command-bare"
    _write_setup(bare_project, executable, prefix=("deepsearch-axi",))
    _, absent = _home(base_env, bare_project, executable)
    assert absent["integrations"] == {
        "claude-code": "stale",
        "codex": "stale",
        "opencode": "stale",
    }

    # The same bare prefix is trustworthy only when PATH really resolves it to
    # the running console script.
    path_dir = tmp_path / "pathbin"
    path_dir.mkdir()
    os.symlink(executable, path_dir / "deepsearch-axi")
    on_path = dict(
        base_env,
        PATH=os.pathsep.join((str(path_dir), env.get("PATH", ""))),
    )
    _, present = _home(on_path, bare_project, executable)
    assert present["integrations"] == {
        "claude-code": "installed",
        "codex": "installed",
        "opencode": "installed",
    }


def _malformed_home_run(
    env: dict[str, str],
    project: Path,
    executable: Path,
    *,
    manifest: bytes | None = None,
    state: bytes | None = None,
) -> tuple[int, str, str]:
    """Run home over a project whose owned records are arbitrary bytes."""
    owned = project / ".deepsearch-axi"
    owned.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (owned / "manifest.json").write_bytes(manifest)
    if state is not None:
        (owned / "state.json").write_bytes(state)
    run = run_cli([], env=env, cwd=project, executable=[str(executable)])
    return run.code, run.stdout, run.stderr


_MALFORMED_MANIFESTS: tuple[bytes, ...] = (
    b"[]",
    b'{"hosts": []}',
    b'{"hosts": {"codex": 5}}',
    b'{"hosts": {"codex": {"files": "abc", "commands": "zz"}}}',
    b'{"project": null, "executable": null, "command": 7, "hosts": {"codex": {"files": [1]}}}',
    b'{"hosts": {"opencode": {"files": [".opencode/plugins/deepsearch-axi.js"], "commands": {}}}}',
    b"\xff\xfe{",
    b'{"hosts": ',
)

_MALFORMED_STATES: tuple[bytes, ...] = (
    b"[1]",
    b'{"schema": 1, "seq": 1, "sessions": 5, "scopes": {}}',
    b'{"schema": 1, "seq": 1, "sessions": [], "scopes": [1, 2]}',
    b'{"schema": 1, "seq": 1, "sessions": [], "scopes": {".": {"search_ok": "x"}}}',
    b'{"schema": 1, "seq": 1, "sessions": [null, 3], "scopes": {}}',
    b"\xff\xfe{",
)


def _valid_manifest_bytes(project: Path, executable: Path) -> bytes:
    return manifest_bytes(
        Manifest(
            project=str(project.resolve()),
            executable=str(executable),
            command=(str(executable),),
            hosts={},
        )
    )


def test_home_contains_malformed_records(
    env: dict[str, str], home_dir: Path, tmp_path: Path
) -> None:
    """Any malformed owned record degrades to a readable home view, never a failure."""
    executable = _fake_console(home_dir)
    # Manifest cases replace the manifest outright; state cases keep a valid
    # manifest bound to their own project so the state file is really read.
    cases: list[tuple[str, bytes | None, bytes | None]] = [
        (f"manifest-{index}", raw, None) for index, raw in enumerate(_MALFORMED_MANIFESTS)
    ]
    for index, raw in enumerate(_MALFORMED_STATES):
        cases.append((f"state-{index}", None, raw))
    for label, manifest, state in cases:
        project = tmp_path / label
        project.mkdir()
        if manifest is None:
            manifest = _valid_manifest_bytes(project, executable)
        code, stdout, stderr = _malformed_home_run(
            dict(env, DEEPSEEK_API_KEY=FAKE_KEY),
            project,
            executable,
            manifest=manifest,
            state=state,
        )
        assert code == 0, (label, stdout, stderr)
        assert stderr == ""
        document = decode(stdout)
        assert document["bin"] == "~/bin/deepsearch-axi", label
        assert "integrations" in document, label
        assert FAKE_KEY not in stdout, label


def test_home_contains_deeply_nested_records(
    stub: Callable[..., StubServer],
    env: dict[str, str],
    home_dir: Path,
    tmp_path: Path,
) -> None:
    """A recursion-exhausting owned record must not turn any command into a crash.

    Home is documented to always succeed. Deep nesting raises RecursionError
    while the manifest is validated during state-root discovery, which the home
    probe and the post-request research counter must both contain.
    """
    server = stub(lambda request: json_response({"results": []}))
    executable = _fake_console(home_dir)
    deep = b"[" * 100_000

    deep_manifest_project = tmp_path / "deep-manifest"
    deep_manifest_project.mkdir()
    code, stdout, stderr = _malformed_home_run(
        dict(env, DEEPSEEK_API_KEY=FAKE_KEY),
        deep_manifest_project,
        executable,
        manifest=deep,
    )
    assert code == 0, ("deep-manifest", stdout[:200], stderr)
    assert "internal_error" not in stdout, "deep-manifest"

    # The research counter runs in a finally block after a billable request;
    # a hostile record must never leak a traceback or change the result.
    search = run_cli(
        ["search", "nested"],
        env=dict(
            env,
            DEEPSEEK_API_KEY=FAKE_KEY,
            DEEPSEEK_BASE_URL=server.base_url,
        ),
        cwd=deep_manifest_project,
        executable=[str(executable)],
    )
    assert search.code == 0, (search.stdout[:200], search.stderr)
    assert search.stderr == ""
    assert "Traceback" not in search.stderr

    deep_state_project = tmp_path / "deep-state"
    deep_state_project.mkdir()
    code, stdout, stderr = _malformed_home_run(
        dict(env, DEEPSEEK_API_KEY=FAKE_KEY),
        deep_state_project,
        executable,
        manifest=_valid_manifest_bytes(deep_state_project, executable),
        state=deep,
    )
    assert code == 0, ("deep-state", stdout[:200], stderr)
    assert "internal_error" not in stdout, "deep-state"


_HELP_CASES: tuple[tuple[list[str], str, str, tuple[str, ...]], ...] = (
    (
        ["--help"],
        "deepsearch-axi",
        "deepsearch-axi [command] [flags]",
        ("-v, -V, --version", "--help"),
    ),
    (
        ["search", "--help"],
        "deepsearch-axi search",
        "deepsearch-axi search <query> [flags]",
        (
            "--count N",
            "--need-content",
            "--ori-question TEXT",
            "--localization IP",
            "--fields FIELD,...",
            "--full",
            "--help",
        ),
    ),
    (
        ["fetch", "--help"],
        "deepsearch-axi fetch",
        "deepsearch-axi fetch <url> [flags]",
        ("--full", "--help"),
    ),
    (
        ["setup", "--help"],
        "deepsearch-axi setup",
        "deepsearch-axi setup [flags]",
        ("--agent NAME", "--help"),
    ),
    (
        ["session", "--help"],
        "deepsearch-axi session",
        "deepsearch-axi session <start|end> --agent NAME --project ABS_DIR",
        ("--help",),
    ),
    (
        ["session", "start", "--help"],
        "deepsearch-axi session start",
        "deepsearch-axi session start --agent NAME --project ABS_DIR",
        ("--agent NAME", "--project ABS_DIR", "--help"),
    ),
    (
        ["session", "end", "--help"],
        "deepsearch-axi session end",
        "deepsearch-axi session end --agent NAME --project ABS_DIR",
        ("--agent NAME", "--project ABS_DIR", "--help"),
    ),
)


def test_command_help(
    stub: Callable[..., StubServer],
    env: dict[str, str],
    home_dir: Path,
    project_dir: Path,
) -> None:
    server = stub(lambda request: json_response({"unexpected": True}))
    cwd_dotenv = project_dir / ".env"
    home_dotenv = home_dir / USER_DOTENV
    cwd_dotenv.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    home_dotenv.parent.mkdir(parents=True)
    home_dotenv.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    environment = dict(env, DEEPSEEK_API_KEY=FAKE_KEY, DEEPSEEK_BASE_URL=server.base_url)
    config_paths = []
    for relative, content in (
        (Path(".claude") / "settings.json", '{"hooks": {}}'),
        (Path(".codex") / "hooks.json", '{"hooks": {}}'),
        (Path(".codex") / "config.toml", "[features]\nhooks = true\n"),
        (
            Path(".opencode") / "plugins" / "deepsearch-axi.js",
            "// managed-by: deepsearch-axi\n",
        ),
        (MANIFEST_RELATIVE, "{}"),
        (Path(".deepsearch-axi") / "state.json", "{}"),
    ):
        path = project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        config_paths.append(path)
    watched = [cwd_dotenv, home_dotenv, *config_paths]
    before_project = _snapshot(project_dir)
    before_home = _snapshot(home_dir)

    for args, command, usage, expected_flags in _HELP_CASES:
        run = run_cli(args, env=environment, cwd=project_dir, read_guard=watched)
        assert run.code == 0, (args, run.stdout, run.stderr)
        assert run.stderr == ""
        assert len(run.stdout) <= 4000
        document = decode(run.stdout)
        assert document["command"] == command
        assert document["usage"] == usage
        assert document["description"]
        flags = document["flags"]
        assert tuple(row["flag"] for row in flags) == expected_flags
        assert all(row["description"] for row in flags)
        examples = document["examples"]
        assert 2 <= len(examples) <= 3
        assert all(example.startswith("deepsearch-axi") for example in examples)
        # Only implemented grammar: no JSON mode, key flag, or invented options.
        for absent in ("--json", "--api-key", "--output", "--model", "--url"):
            assert absent not in run.stdout
        assert FAKE_KEY not in run.stdout + run.stderr
        assert run.reads == ()
        assert server.requests == []

    root = decode(run_cli(["--help"], env=environment, cwd=project_dir).stdout)
    assert [entry["name"] for entry in root["commands"]] == [
        "search",
        "fetch",
        "setup",
        "session",
    ]
    search = decode(run_cli(["search", "--help"], env=environment, cwd=project_dir).stdout)
    assert [entry["name"] for entry in search["arguments"]] == ["query"]
    assert {row["flag"]: row["default"] for row in search["flags"]} == {
        "--count N": "10",
        "--need-content": "false",
        "--ori-question TEXT": "omitted",
        "--localization IP": "",
        "--fields FIELD,...": "title,url,snippet",
        "--full": "false",
        "--help": "false",
    }
    fetch = decode(run_cli(["fetch", "--help"], env=environment, cwd=project_dir).stdout)
    assert [entry["name"] for entry in fetch["arguments"]] == ["url"]
    assert {row["flag"]: row["default"] for row in fetch["flags"]} == {
        "--full": "false",
        "--help": "false",
    }
    setup = decode(run_cli(["setup", "--help"], env=environment, cwd=project_dir).stdout)
    assert {row["flag"]: row["default"] for row in setup["flags"]} == {
        "--agent NAME": "all",
        "--help": "false",
    }
    session = decode(run_cli(["session", "--help"], env=environment, cwd=project_dir).stdout)
    assert [entry["name"] for entry in session["commands"]] == ["start", "end"]
    for path in (["session", "start"], ["session", "end"]):
        subcommand = decode(run_cli([*path, "--help"], env=environment, cwd=project_dir).stdout)
        assert {row["flag"]: row["default"] for row in subcommand["flags"]} == {
            "--agent NAME": "required",
            "--project ABS_DIR": "required",
            "--help": "false",
        }

    assert _snapshot(project_dir) == before_project
    assert _snapshot(home_dir) == before_home
