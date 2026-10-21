"""AC-24: version fast path output, isolation, metadata parity and timing."""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

import tomlkit

from deepsearch_axi._version import VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
ALIASES = ("-v", "-V", "--version")


def _run(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    executable: list[str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = executable or [sys.executable, "-m", "deepsearch_axi"]
    return subprocess.run(
        command + args,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )


def _project_version() -> str:
    document = tomlkit.parse((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(document["project"]["version"])


def _expected() -> bytes:
    return (_project_version() + "\n").encode("utf-8")


def _declared_version() -> str:
    try:
        return metadata.version("deepsearch-axi")
    except metadata.PackageNotFoundError:
        return _project_version()


def test_version_constant_matches_metadata() -> None:
    assert VERSION == _declared_version()
    assert VERSION == _project_version()


def test_source_tree_entrypoint_reports_the_project_version(
    env: dict[str, str], tmp_path: Path
) -> None:
    origin = subprocess.run(
        [sys.executable, "-c", "import deepsearch_axi; print(deepsearch_axi.__file__)"],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert origin.returncode == 0, origin.stderr
    assert SRC_DIR in Path(origin.stdout.decode("utf-8").strip()).resolve().parents
    completed = _run(["--version"], env=env, cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == _expected()
    assert completed.stderr == b""


def test_source_tree_version_comes_from_pyproject(env: dict[str, str], tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    shutil.copytree(SRC_DIR, checkout / "src", ignore=shutil.ignore_patterns("__pycache__"))
    project = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = re.sub(r'(?m)^version = "[^"]+"$', 'version = "9.8.7"', project, count=1)
    assert project.count('version = "9.8.7"') == 1
    (checkout / "pyproject.toml").write_text(project, encoding="utf-8")
    source_env = dict(
        env,
        PYTHONPATH=os.pathsep.join((str(checkout / "src"), env["PYTHONPATH"])),
    )
    completed = _run(["--version"], env=source_env, cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b"9.8.7\n"
    assert completed.stderr == b""


def test_sole_version_aliases_print_exact_bytes(env: dict[str, str], tmp_path: Path) -> None:
    for alias in ALIASES:
        completed = _run([alias], env=env, cwd=tmp_path)
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == _expected()
        assert completed.stderr == b""


def test_console_script_version_when_installed(env: dict[str, str], tmp_path: Path) -> None:
    candidate = Path(sys.executable).with_name("deepsearch-axi")
    if not candidate.exists():
        return
    script = str(candidate)
    for alias in ALIASES:
        completed = _run([alias], env=env, cwd=tmp_path, executable=[script])
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == _expected()
        assert completed.stderr == b""


def test_version_fast_path_imports_no_product_modules(env: dict[str, str], tmp_path: Path) -> None:
    code = "\n".join(
        [
            "import sys",
            "from deepsearch_axi import _entry",
            "code = _entry.main(['--version'])",
            "allowed = {'deepsearch_axi._entry', 'deepsearch_axi._version'}",
            "forbidden = sorted(",
            "    name for name in sys.modules",
            "    if name in {'toon_format', 'tomlkit'}",
            "    or (name.startswith('deepsearch_axi.') and name not in allowed)",
            ")",
            "assert not forbidden, forbidden",
            "raise SystemExit(code)",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=dict(env, DEEPSEEK_API_KEY="sk-test-MARKER-ignored"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == _expected()
    assert b"MARKER" not in completed.stdout + completed.stderr


def test_version_fast_path_reads_no_config(env: dict[str, str], tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("DEEPSEEK_API_KEY=from-cwd-dotenv\n", encoding="utf-8")
    home_config = tmp_path / "home" / ".config" / "deepsearch-axi"
    home_config.mkdir(parents=True)
    (home_config / ".env").write_text("DEEPSEEK_API_KEY=from-home-dotenv\n", encoding="utf-8")
    completed = _run(["--version"], env=dict(env, DEEPSEEK_API_KEY="from-environment"), cwd=cwd)
    assert completed.returncode == 0
    assert completed.stdout == _expected()
    assert b"from-" not in completed.stdout + completed.stderr


def test_version_alias_not_alone_is_not_the_fast_path(env: dict[str, str], tmp_path: Path) -> None:
    for args in (["--version", "extra"], ["-v", "search"], ["search", "q", "-v"]):
        completed = _run(args, env=env, cwd=tmp_path)
        assert completed.stdout != _expected()
        assert completed.returncode != 0


def _timed(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    samples: int,
) -> list[float]:
    times = []
    for _ in range(samples):
        start = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
        times.append(time.perf_counter() - start)
        assert completed.returncode == 0, completed.stderr
    return times


def test_version_startup_budget(env: dict[str, str], tmp_path: Path) -> None:
    samples = 25
    version_command = [sys.executable, "-m", "deepsearch_axi", "--version"]
    baseline_command = [sys.executable, "-c", "print(1)"]
    _timed(version_command, env=env, cwd=tmp_path, samples=1)
    _timed(baseline_command, env=env, cwd=tmp_path, samples=1)
    version_times = _timed(version_command, env=env, cwd=tmp_path, samples=samples)
    baseline_times = _timed(baseline_command, env=env, cwd=tmp_path, samples=samples)
    version_median = statistics.median(version_times)
    baseline_median = statistics.median(baseline_times)
    budget = 3 * baseline_median + 0.020
    report = {
        "samples": samples,
        "version_samples_seconds": version_times,
        "baseline_samples_seconds": baseline_times,
        "version_median_seconds": version_median,
        "baseline_median_seconds": baseline_median,
        "budget_seconds": budget,
    }
    retained = tmp_path / "version-timing.json"
    retained.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert version_median <= budget, (
        f"version median {version_median:.4f}s exceeded budget {budget:.4f}s "
        f"(baseline median {baseline_median:.4f}s); samples retained at {retained}"
    )
