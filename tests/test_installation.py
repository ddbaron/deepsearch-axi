"""AC-02: offline installation through pip, pipx and uv tool.

Every installer receives a temporary HOME, PIPX_HOME/UV_TOOL_DIR and a local
wheelhouse, then the installed console script runs ``--version``, the no-arg
home view and one fake-key search against a loopback stub. A snapshot of the
operator tool directories proves nothing was written outside the temp tree.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import toon_format
from support.env import FAKE_KEY
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
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_NO_PROGRESS": "1",
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


def _download_dependencies(destination: Path, work: Path) -> None:
    env = _clean_env(work / "home", PIP_CACHE_DIR=str(work / "pip-cache"))
    completed = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            destination,
            "toon-format==1.2.0",
            "tomlkit>=0.13.3,<1",
        ],
        env,
        timeout=300,
    )
    if completed.returncode != 0:
        pytest.fail(
            "offline installation tests need a pre-provisioned wheelhouse "
            "(DEEPSEARCH_AXI_WHEELHOUSE or ./wheelhouse) containing toon-format "
            "and tomlkit wheels, and automatic download failed:\n"
            f"{completed.stdout}\n{completed.stderr}",
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
        _download_dependencies(destination, work)

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
def pipx_path(tmp_path_factory: pytest.TempPathFactory, wheelhouse: Path) -> Path:
    found = shutil.which("pipx")
    if found:
        return Path(found)
    work = tmp_path_factory.mktemp("pipx-tool")
    python = _create_venv(work / "venv", work / "home")
    env = _clean_env(work / "home", PIP_CACHE_DIR=str(work / "pip-cache"))
    offline = _run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            wheelhouse,
            "pipx",
        ],
        env,
        timeout=300,
    )
    if offline.returncode != 0:
        completed = _run(
            [python, "-m", "pip", "install", "pipx"],
            env,
            timeout=300,
        )
        if completed.returncode != 0:
            pytest.fail(
                "pipx is required for the installation tests; CI installs it and "
                "this environment could not provision it:\n"
                f"{completed.stdout}\n{completed.stderr}",
                pytrace=False,
            )
    executable = work / "venv" / "bin" / "pipx"
    assert executable.is_file(), "pipx provisioning did not create an executable"
    return executable


@pytest.fixture(scope="module")
def uv_path() -> Path:
    found = shutil.which("uv")
    if found is None:
        pytest.fail(
            "uv is required for the installation tests; CI installs it before pytest",
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

    assert _tool_dir_snapshot() == before, "installation wrote outside its temp homes"
