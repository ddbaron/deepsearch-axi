"""AC-02: offline installation through pip, pipx and uv tool.

Every installer receives a temporary HOME, PIPX_HOME/UV_TOOL_DIR and a local
pre-provisioned wheelhouse, then the installed console script runs
``--version``, the no-arg home view and one fake-key search against a loopback
stub. Child processes inherit only the sitecustomize network guard (never the
source tree), and a snapshot of the operator tool directories proves nothing
was written outside the temp tree. Missing wheels or installer tools fail
loudly instead of reaching for the public network.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import toon_format
from support.env import FAKE_KEY, SITECUSTOMIZE_DIR
from support.stub_server import StubServer, json_response

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = ("pip", "pipx", "uv")
SEARCH_PAYLOAD = {
    "results": [
        {
            "title": "Offline installation result",
            "url": "https://example.com/offline",
            "snippet": "Loopback only.",
            "summary": "",
            "content": "",
            "authority": 1,
            "rerank_score": 0.5,
        }
    ]
}


def _clean_env(home: Path, **overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        # Only the harness guard — never the source package — is importable so
        # installed executables prove the wheel itself is what runs.
        "PYTHONPATH": str(SITECUSTOMIZE_DIR),
        "DEEPSEARCH_AXI_NETGUARD": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_NO_INDEX": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_NO_PROGRESS": "1",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    env.update({name: str(value) for name, value in overrides.items()})
    return env


def _run(
    command: list[object],
    env: dict[str, str],
    *,
    cwd: Path | None = None,
    timeout: int = 240,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=None if cwd is None else str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed


def _require_success(completed: subprocess.CompletedProcess[str], label: str) -> None:
    if completed.returncode == 0:
        return
    pytest.fail(
        f"{label} failed with exit {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        pytrace=False,
    )


@pytest.fixture(scope="module")
def project_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("installation-dist")
    env = _clean_env(tmp_path_factory.mktemp("installation-build-home"))
    completed = _run(
        [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", out],
        env,
        cwd=REPO_ROOT,
        timeout=300,
    )
    _require_success(completed, "python -m build --wheel")
    wheels = sorted(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found {wheels}"
    return wheels[0]


def _has_dependency_wheels(directory: Path) -> bool:
    return bool(list(directory.glob("toon_format-*.whl"))) and bool(
        list(directory.glob("tomlkit-*.whl"))
    )


def _wheelhouse_failure(destination: Path) -> None:
    pytest.fail(
        "offline installation tests need a pre-provisioned wheelhouse before "
        "pytest: set DEEPSEARCH_AXI_WHEELHOUSE or create ./wheelhouse with "
        "toon_format-*.whl and tomlkit-*.whl. Provision it with:\n"
        "  python -m pip download --only-binary=:all: --dest wheelhouse "
        'toon-format==1.2.0 "tomlkit>=0.13.3,<1"\n'
        f"Checked: {destination}",
        pytrace=False,
    )


@pytest.fixture(scope="module")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory, project_wheel: Path) -> Path:
    work = tmp_path_factory.mktemp("wheelhouse")
    destination = work / "wheels"
    destination.mkdir()

    configured = os.environ.get("DEEPSEARCH_AXI_WHEELHOUSE")
    sources: list[Path] = []
    if configured:
        candidate = Path(configured)
        if not candidate.is_dir():
            pytest.fail(
                f"DEEPSEARCH_AXI_WHEELHOUSE is not a directory: {candidate}",
                pytrace=False,
            )
        sources.append(candidate)
    repo_wheelhouse = REPO_ROOT / "wheelhouse"
    if repo_wheelhouse.is_dir():
        sources.append(repo_wheelhouse)

    for source in sources:
        for wheel in source.glob("*.whl"):
            if not wheel.name.lower().startswith("deepsearch_axi-"):
                shutil.copy2(wheel, destination / wheel.name)

    if not _has_dependency_wheels(destination):
        _wheelhouse_failure(destination)

    shutil.copy2(project_wheel, destination / project_wheel.name)
    assert _has_dependency_wheels(destination)
    return destination


def _create_venv(path: Path, home: Path) -> Path:
    completed = _run(
        [sys.executable, "-m", "venv", path],
        _clean_env(home, PIP_CACHE_DIR=str(home.parent / "pip-cache")),
        timeout=180,
    )
    _require_success(completed, f"python -m venv {path}")
    python = path / "bin" / "python"
    assert python.is_file(), f"missing venv interpreter: {python}"
    return python


@pytest.fixture(scope="module")
def pipx_path() -> Path:
    found = shutil.which("pipx")
    if found is None:
        pytest.fail(
            "pipx is required for the installation tests; provision it before "
            "pytest (CI installs pipx into the job environment)",
            pytrace=False,
        )
    return Path(found)


@pytest.fixture(scope="module")
def uv_path() -> Path:
    found = shutil.which("uv")
    if found is None:
        pytest.fail(
            "uv is required for the installation tests; provision it before "
            "pytest (CI installs uv into the job environment)",
            pytrace=False,
        )
    return Path(found)


def _install_pip(tmp: Path, wheelhouse: Path) -> Path:
    python = _create_venv(tmp / "venv", tmp / "home")
    env = _clean_env(tmp / "home", PIP_CACHE_DIR=str(tmp / "pip-cache"))
    completed = _run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            wheelhouse,
            "deepsearch-axi==0.1.0",
        ],
        env,
        timeout=300,
    )
    _require_success(completed, "pip wheel installation")
    return tmp / "venv" / "bin" / "deepsearch-axi"


def _install_pipx(tmp: Path, wheelhouse: Path, pipx: Path, wheel: Path) -> Path:
    bin_dir = tmp / "pipx-bin"
    env = _clean_env(
        tmp / "home",
        PIPX_HOME=str(tmp / "pipx-home"),
        PIPX_BIN_DIR=str(bin_dir),
        PIPX_DEFAULT_PYTHON=sys.executable,
        PIP_CACHE_DIR=str(tmp / "pip-cache"),
    )
    completed = _run(
        [
            pipx,
            "install",
            "--force",
            "--python",
            sys.executable,
            "--pip-args",
            f"--no-index --find-links {wheelhouse}",
            wheel,
        ],
        env,
        timeout=300,
    )
    _require_success(completed, "pipx wheel installation")
    return bin_dir / "deepsearch-axi"


def _install_uv(tmp: Path, wheelhouse: Path, uv: Path, wheel: Path) -> Path:
    bin_dir = tmp / "uv-bin"
    env = _clean_env(
        tmp / "home",
        UV_TOOL_DIR=str(tmp / "uv-tools"),
        UV_TOOL_BIN_DIR=str(bin_dir),
        UV_CACHE_DIR=str(tmp / "uv-cache"),
    )
    completed = _run(
        [
            uv,
            "tool",
            "install",
            "--force",
            "--offline",
            "--no-index",
            "--find-links",
            wheelhouse,
            "--python",
            sys.executable,
            wheel,
        ],
        env,
        timeout=300,
    )
    _require_success(completed, "uv tool wheel installation")
    return bin_dir / "deepsearch-axi"


def _real_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path.home()


def _tool_dir_snapshot() -> dict[str, tuple[int, int, int] | None]:
    home = _real_home()
    candidates = (
        home / ".local",
        home / ".local" / "bin",
        home / ".local" / "pipx",
        home / ".local" / "share" / "uv",
        home / ".cache",
        home / ".cache" / "pip",
        home / ".cache" / "uv",
        home / ".config" / "pip",
    )
    snapshot: dict[str, tuple[int, int, int] | None] = {}
    for path in candidates:
        try:
            status = path.stat()
        except OSError:
            snapshot[str(path)] = None
            continue
        snapshot[str(path)] = (status.st_mode, status.st_size, status.st_mtime_ns)
    return snapshot


def _install(
    installer: str,
    tmp: Path,
    *,
    project_wheel: Path,
    wheelhouse: Path,
    pipx: Path,
    uv: Path,
) -> Path:
    if installer == "pip":
        return _install_pip(tmp, wheelhouse)
    if installer == "pipx":
        return _install_pipx(tmp, wheelhouse, pipx, project_wheel)
    return _install_uv(tmp, wheelhouse, uv, project_wheel)


def _script_python(executable: Path) -> Path:
    """Resolve the console script preamble to the installed tool interpreter."""
    lines = executable.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].startswith("#!"), lines[:1]
    candidates: list[str] = []
    parts = shlex.split(lines[0][2:].strip())
    if Path(parts[0]).name == "env":
        candidates.extend(part for part in parts[1:] if not part.startswith("-"))
    elif Path(parts[0]).name != "sh":
        candidates.append(parts[0])
    # pipx rewrites console scripts as a /bin/sh trampoline that execs the
    # private venv interpreter on the following line.
    for line in lines[:3]:
        match = re.search(r"'''exec'\s+'([^']+)'", line)
        if match:
            candidates.append(match.group(1))
    for candidate in candidates:
        interpreter = Path(candidate)
        if interpreter.is_file():
            return interpreter
    pytest.fail(f"installed script has an unusable interpreter: {lines[:3]}", pytrace=False)


_NETGUARD_PROBE = "import socket\nsocket.create_connection(('192.0.2.1', 80), timeout=2)\n"


def _closed_loopback_url() -> str:
    """Return an http origin on a loopback port nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _assert_installed_process_guards(executable: Path, env: dict[str, str], cwd: Path) -> None:
    """Prove the installed interpreter imports the wheel and blocks non-loopback."""
    interpreter = _script_python(executable)
    probe = _run(
        [
            interpreter,
            "-c",
            "import deepsearch_axi, sys; print(deepsearch_axi.__file__); print(sys.path)",
        ],
        env,
        cwd=cwd,
    )
    _require_success(probe, "installed deepsearch_axi import probe")
    lines = probe.stdout.splitlines()
    module = Path(lines[0]).resolve()
    source_dir = (REPO_ROOT / "src").resolve()
    assert source_dir not in module.parents, f"probe imported the source tree: {module}"
    assert str(source_dir) not in lines[1], lines[1]

    guarded = _run([interpreter, "-c", _NETGUARD_PROBE], env, cwd=cwd, timeout=30)
    assert guarded.returncode != 0, "non-loopback connect unexpectedly succeeded"
    assert "blocked by the test guard" in guarded.stderr, guarded.stderr


@pytest.mark.parametrize("installer", INSTALLERS)
def test_offline_installation_and_runs(
    installer: str,
    tmp_path: Path,
    project_wheel: Path,
    wheelhouse: Path,
    pipx_path: Path,
    uv_path: Path,
    stub,
) -> None:
    before = _tool_dir_snapshot()
    executable = _install(
        installer,
        tmp_path,
        project_wheel=project_wheel,
        wheelhouse=wheelhouse,
        pipx=pipx_path,
        uv=uv_path,
    )
    assert executable.is_file(), f"{installer} did not install a console script"

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    base_env = _clean_env(home)

    version = _run([executable, "--version"], base_env, cwd=project)
    assert version.returncode == 0, version.stderr
    assert version.stdout == "0.1.0\n"
    assert version.stderr == ""

    _assert_installed_process_guards(executable, base_env, project)

    home_env = _clean_env(home, DEEPSEEK_API_KEY=FAKE_KEY)
    home_run = _run([executable], home_env, cwd=project)
    assert home_run.returncode == 0, home_run.stderr
    home_document = toon_format.decode(home_run.stdout, strict=True)
    assert home_document["description"]
    assert home_document["cwd"] == str(project)
    assert home_document["auth"]["state"] == "configured"
    assert home_document["integrations"] == {
        "claude-code": "missing",
        "codex": "missing",
        "opencode": "missing",
    }
    assert FAKE_KEY not in home_run.stdout
    assert FAKE_KEY not in home_run.stderr

    with StubServer(lambda request: json_response(SEARCH_PAYLOAD)) as server:
        search_env = _clean_env(
            home,
            DEEPSEEK_API_KEY=FAKE_KEY,
            DEEPSEEK_BASE_URL=server.base_url,
        )
        search = _run(
            [executable, "search", "offline installation probe"],
            search_env,
            cwd=project,
        )

    assert search.returncode == 0, search.stderr
    document = toon_format.decode(search.stdout, strict=True)
    assert document["query"] == "offline installation probe"
    assert document["count"] == 1
    assert document["results"][0]["url"] == "https://example.com/offline"
    assert FAKE_KEY not in search.stdout
    assert FAKE_KEY not in search.stderr
    assert len(server.requests) == 1
    captured = server.requests[0]
    assert captured.path == "/v1/tools/search"
    assert captured.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert captured.json["query"] == "offline installation probe"

    # A refused loopback connection maps to transport_error everywhere. A
    # blackholed non-loopback address cannot: pipx appends -E to console-script
    # shebangs, so the PYTHONPATH-mounted harness guard is not imported there
    # and the child instead spends a real 30s connect timeout reporting
    # ``timeout``.
    blocked_env = _clean_env(
        home,
        DEEPSEEK_API_KEY=FAKE_KEY,
        DEEPSEEK_BASE_URL=_closed_loopback_url(),
    )
    blocked = _run([executable, "search", "guarded probe"], blocked_env, cwd=project)
    assert blocked.returncode == 1, blocked.stderr
    blocked_document = toon_format.decode(blocked.stdout, strict=True)
    assert blocked_document["error"]["code"] == "transport_error"
    assert FAKE_KEY not in blocked.stdout
    assert FAKE_KEY not in blocked.stderr

    # Setup under the installed console script must record that exact
    # executable (the installer's bin dir is not on PATH here) and the generated
    # Claude Code start hook must execute it and report the cwd-scoped context.
    setup_project = (tmp_path / "setup-project").resolve()
    setup_project.mkdir()
    setup_run = _run([executable, "setup", "--agent", "claude-code"], base_env, cwd=setup_project)
    assert setup_run.returncode == 0, setup_run.stderr
    assert setup_run.stderr == ""
    setup_document = toon_format.decode(setup_run.stdout, strict=True)
    assert setup_document["setup"] == {"project": str(setup_project), "changed": True}
    manifest = json.loads(
        (setup_project / ".deepsearch-axi" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["executable"] == str(executable)
    assert manifest["command"] == [str(executable)]
    settings_text = (setup_project / ".claude" / "settings.json").read_text(encoding="utf-8")
    settings = json.loads(settings_text)
    start = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert shlex.split(start)[0] == str(executable)
    hook = subprocess.run(
        shlex.split(start),
        cwd=str(setup_project),
        env=home_env,
        input=json.dumps(
            {
                "session_id": f"installed-{installer}",
                "cwd": str(setup_project),
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
        ).encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    assert hook.returncode == 0, hook.stderr
    assert hook.stderr == b""
    hook_document = toon_format.decode(hook.stdout.decode("utf-8"), strict=True)
    assert hook_document["cwd"] == str(setup_project)
    assert hook_document["integration"] == "installed"
    assert FAKE_KEY not in hook.stdout.decode("utf-8")

    assert _tool_dir_snapshot() == before, "installation wrote outside its temp homes"
