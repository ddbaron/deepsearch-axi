"""AC-01: distribution metadata, entrypoints, package data and size budget.

The tests build the real wheel and sdist in a temporary directory (no
isolation, using the pinned hatchling dev dependency) and inspect the built
artifacts rather than trusting the source layout.
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
import tomlkit
from support.env import SITECUSTOMIZE_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
WHEEL_LIMIT = 153_600
WHEEL_DATA = (
    "deepsearch_axi/guidance.py",
    "deepsearch_axi/templates/opencode_plugin.js",
)
FORBIDDEN_PARTS = frozenset(
    {
        ".env",
        ".deepsearch-axi",
        ".git",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "secrets",
        "state.json",
        "wheelhouse",
    }
)


def _clean_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PYTHONPATH": str(SITECUSTOMIZE_DIR),
        "DEEPSEARCH_AXI_NETGUARD": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@pytest.fixture(scope="module")
def distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    out = tmp_path_factory.mktemp("dist")
    env = _clean_env(tmp_path_factory.mktemp("build-home"))
    completed = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(out)],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        pytest.fail(
            "python -m build --no-isolation failed; install requirements-dev.txt "
            f"(hatchling, build, twine)\n{completed.stdout}\n{completed.stderr}",
            pytrace=False,
        )
    wheels = sorted(out.glob("*.whl"))
    sdists = sorted(out.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected one wheel, found {wheels}"
    assert len(sdists) == 1, f"expected one sdist, found {sdists}"
    return wheels[0], sdists[0]


def _members(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _metadata(files: dict[str, bytes]) -> object:
    name = next(name for name in files if name.endswith(".dist-info/METADATA"))
    return Parser().parsestr(files[name].decode("utf-8"))


def _entrypoints(files: dict[str, bytes]) -> configparser.ConfigParser:
    name = next(name for name in files if name.endswith(".dist-info/entry_points.txt"))
    parser = configparser.ConfigParser()
    parser.read_string(files[name].decode("utf-8"))
    return parser


def test_metadata_contract(distributions: tuple[Path, Path]) -> None:
    wheel, _ = distributions
    files = _members(wheel)
    metadata = _metadata(files)

    document = tomlkit.parse((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    assert str(project["name"]) == "deepsearch-axi"
    assert str(project["version"]) == "0.1.0"
    assert str(project["requires-python"]) == ">=3.10"
    assert str(project["license"]) == "MIT"
    assert dict(project["scripts"]) == {"deepsearch-axi": "deepsearch_axi._entry:main"}
    assert dict(project["urls"])["Repository"] == "https://github.com/ddbaron/deepsearch-axi"

    assert metadata["Name"] == "deepsearch-axi"  # type: ignore[index]
    assert metadata["Version"] == "0.1.0"  # type: ignore[index]
    assert metadata["Requires-Python"] == ">=3.10"  # type: ignore[index]
    license_text = metadata["License-Expression"] or metadata["License"]  # type: ignore[index]
    assert license_text == "MIT"

    requirements = metadata.get_all("Requires-Dist") or []  # type: ignore[attr-defined]
    assert len(requirements) == 2, requirements
    normalized = {requirement.split(";", 1)[0].strip() for requirement in requirements}
    assert "toon-format==1.2.0" in normalized
    assert any(requirement.startswith("tomlkit") for requirement in normalized)
    assert all(
        requirement.lower().startswith(("toon-format", "tomlkit")) for requirement in normalized
    )


def test_entrypoints_and_packaged_data(distributions: tuple[Path, Path]) -> None:
    wheel, sdist = distributions
    files = _members(wheel)
    entrypoints = _entrypoints(files)
    assert entrypoints["console_scripts"]["deepsearch-axi"] == "deepsearch_axi._entry:main"
    assert "deepsearch_axi/__main__.py" in files
    assert "deepsearch_axi/_entry.py" in files
    for name in WHEEL_DATA:
        assert name in files, f"missing packaged data: {name}"
    template = files["deepsearch_axi/templates/opencode_plugin.js"].decode("utf-8")
    assert "managed-by: deepsearch-axi" in template
    assert "__DEEPSEARCH_AXI_COMMAND__" in template
    assert "__DEEPSEARCH_AXI_PROJECT__" in template
    guidance = files["deepsearch_axi/guidance.py"].decode("utf-8")
    assert "def skill_markdown(" in guidance

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = archive.getnames()
    joined = "\n".join(sdist_names)
    assert "src/deepsearch_axi/templates/opencode_plugin.js" in joined
    assert "src/deepsearch_axi/guidance.py" in joined
    assert "README.md" in joined
    assert "LICENSE" in joined
    assert "pyproject.toml" in joined


def test_distributions_exclude_secrets_and_artifacts(
    distributions: tuple[Path, Path],
) -> None:
    wheel, sdist = distributions
    wheel_names = set(_members(wheel))
    for name in wheel_names:
        parts = set(Path(name).parts)
        assert not parts & FORBIDDEN_PARTS, f"forbidden wheel member: {name}"
        assert not name.endswith((".pyc", ".pem", ".key")), name
    assert not any(name.startswith("tests/") for name in wheel_names)

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = archive.getnames()
    for name in sdist_names:
        relative = name.split("/", 1)[1] if "/" in name else ""
        parts = set(Path(relative).parts)
        assert not parts & FORBIDDEN_PARTS, f"forbidden sdist member: {name}"
        assert not name.endswith((".pyc", ".pem", ".key")), name

    for name, data in _members(wheel).items():
        if not name.endswith((".py", ".txt", ".md", ".js", ".json")):
            continue
        text = data.decode("utf-8", "replace")
        assert "sk-test-" not in text, name
        assert "BEGIN PRIVATE KEY" not in text, name


def test_wheel_size_budget(distributions: tuple[Path, Path]) -> None:
    wheel, _ = distributions
    size = wheel.stat().st_size
    assert size <= WHEEL_LIMIT, f"wheel is {size} bytes, limit is {WHEEL_LIMIT}"
    assert size > 0
