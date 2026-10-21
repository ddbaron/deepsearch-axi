"""AC-22/23: executable home readiness and spec-driven command help.

The home view is exercised through a real console-script child process so its
``bin`` identity, live integration inspection, scoped prior activity and size
budgets match installed behavior. Help is exercised for every implemented
command path with an audit hook proving no credential/config-file reads.
"""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Callable
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
            "SessionStart": [{"hooks": [{"type": "command", "command": start}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": end}]}],
        }
    }


def _lifecycle_commands(executable: Path, project: Path, agent: str) -> tuple[str, str]:
    prefix = [str(executable)]
    root = str(project.resolve())
    start = shlex.join([*prefix, "session", "start", "--agent", agent, "--project", root])
    end = shlex.join([*prefix, "session", "end", "--agent", agent, "--project", root])
    return start, end


def _plugin_text(executable: Path, project: Path) -> str:
    text = PLUGIN_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("__DEEPSEARCH_AXI_COMMAND__", json.dumps([str(executable)]))
    text = text.replace("__DEEPSEARCH_AXI_PROJECT__", json.dumps(str(project.resolve())))
    return text.replace("__DEEPSEARCH_AXI_VERSION__", json.dumps(VERSION))


def _write_manifest(
    project: Path,
    executable: Path,
    *,
    recorded_executable: str | None = None,
) -> Manifest:
    hosts = {
        "claude-code": ManifestHost(
            files=(".claude/settings.json",),
            commands=_lifecycle_commands(executable, project, "claude-code"),
        ),
        "codex": ManifestHost(
            files=(".codex/hooks.json", ".codex/config.toml"),
            commands=_lifecycle_commands(executable, project, "codex"),
        ),
        "opencode": ManifestHost(
            files=(".opencode/plugins/deepsearch-axi.js",),
            commands=(json.dumps([str(executable)]),),
        ),
    }
    manifest = Manifest(
        project=str(project.resolve()),
        executable=recorded_executable or str(executable),
        command=(str(executable),),
        hosts=hosts,
    )
    owned = project / ".deepsearch-axi"
    owned.mkdir(parents=True, exist_ok=True)
    (owned / "manifest.json").write_bytes(manifest_bytes(manifest))
    return manifest


def _write_setup(project: Path, executable: Path, *, corrupt: str | None = None) -> None:
    """Write a native integration fixture, optionally corrupting one surface."""
    project.mkdir(parents=True, exist_ok=True)
    claude_commands = _lifecycle_commands(executable, project, "claude-code")
    codex_commands = _lifecycle_commands(executable, project, "codex")

    claude_path = project / ".claude" / "settings.json"
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_document = _hook_document(*claude_commands)
    if corrupt == "claude-missing":
        del claude_document["hooks"]["SessionEnd"]
    elif corrupt == "claude-altered":
        claude_document["hooks"]["SessionEnd"][0]["hooks"][0]["command"] += " --debug"
    claude_path.write_text(json.dumps(claude_document, indent=2) + "\n", encoding="utf-8")

    codex_path = project / ".codex" / "hooks.json"
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt == "codex-json":
        codex_path.write_text("{not valid json\n", encoding="utf-8")
    else:
        codex_path.write_text(
            json.dumps(_hook_document(*codex_commands), indent=2) + "\n", encoding="utf-8"
        )

    config_path = project / ".codex" / "config.toml"
    if corrupt == "codex-features":
        config_path.write_text("[features]\nhooks = false\n", encoding="utf-8")
    else:
        config_path.write_text("[features]\nhooks = true\n", encoding="utf-8")

    plugin_path = project / ".opencode" / "plugins" / "deepsearch-axi.js"
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    plugin = _plugin_text(executable, project)
    if corrupt == "plugin-unmanaged":
        plugin = "// local hand-written plugin\n" + plugin.split("\n", 1)[1]
    elif corrupt == "plugin-mismatch":
        plugin = plugin.replace(json.dumps([str(executable)]), '["other-command"]', 1)
    plugin_path.write_text(plugin, encoding="utf-8")

    _write_manifest(
        project,
        executable,
        recorded_executable=(
            "/nonexistent/deepsearch-axi" if corrupt == "stale-executable" else None
        ),
    )
    if corrupt == "claude-missing-file":
        claude_path.unlink()


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
        ("claude-missing", "claude-code"),
        ("claude-altered", "claude-code"),
        ("claude-missing-file", "claude-code"),
        ("plugin-unmanaged", "opencode"),
        ("plugin-mismatch", "opencode"),
    )
    for corrupt, expected_stale in stale_cases:
        project = tmp_path / f"stale-{corrupt}"
        _write_setup(project, executable, corrupt=corrupt)
        _, stale = _home(dict(base_env, DEEPSEEK_API_KEY=FAKE_KEY), project, executable)
        assert stale["integrations"][expected_stale] == "stale", (corrupt, stale["integrations"])
        for agent, state in stale["integrations"].items():
            if agent != expected_stale:
                assert state == "installed", (corrupt, agent, stale["integrations"])

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
    watched = [cwd_dotenv, home_dotenv, project_dir / MANIFEST_RELATIVE]
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
