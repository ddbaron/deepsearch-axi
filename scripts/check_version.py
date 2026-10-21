#!/usr/bin/env python3
"""Fail when the CLI version and ``pyproject.toml`` disagree.

Both checks run a real entry point and compare its output with the single
source, ``[project].version`` in ``pyproject.toml``:

* the source-tree check runs ``python -m deepsearch_axi --version`` with only
  this checkout's ``src`` on ``PYTHONPATH``;
* the wheel check installs a built wheel into a throwaway virtual environment
  and runs its console script.

The wheel check installs with ``--no-deps`` because the bare-version fast path
must not import runtime dependencies; that also keeps the guard offline.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

import tomlkit

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PROJECT = REPO_ROOT / "pyproject.toml"


def project_version() -> str:
    document = tomlkit.parse(PROJECT.read_text(encoding="utf-8"))
    return str(document["project"]["version"])


def _clean_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def _run(command: list[str], *, env: dict[str, str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"version guard: command failed ({completed.returncode}): {command}\n"
            f"{completed.stdout}{completed.stderr}"
        )
    return completed.stdout


def source_tree_version(tmp: Path) -> str:
    env = _clean_env(tmp / "source-home")
    env["PYTHONPATH"] = str(SRC_DIR)
    return _run(
        [sys.executable, "-m", "deepsearch_axi", "--version"],
        env=env,
        cwd=tmp,
    ).strip()


def wheel_version(wheel: Path, tmp: Path) -> str:
    environment = tmp / "venv"
    venv.EnvBuilder(with_pip=True, clear=True, symlinks=os.name != "nt").create(environment)
    bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / "python"
    script = bin_dir / "deepsearch-axi"
    env = _clean_env(tmp / "wheel-home")
    _run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-index", str(wheel)],
        env=env,
        cwd=tmp,
    )
    if not script.is_file():
        raise SystemExit(f"version guard: wheel did not install a console script: {script}")
    return _run([str(script), "--version"], env=env, cwd=tmp).strip()


def _wheel_path() -> Path:
    wheels = sorted((REPO_ROOT / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(
            f"version guard: expected one built wheel in {REPO_ROOT / 'dist'}, found {wheels}; "
            "run python -m build first"
        )
    return wheels[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        type=Path,
        help="built wheel to install; defaults to the single wheel in dist/",
    )
    args = parser.parse_args(argv)
    expected = project_version()
    wheel = args.wheel or _wheel_path()
    with tempfile.TemporaryDirectory(prefix="deepsearch-axi-version-") as directory:
        tmp = Path(directory)
        source = source_tree_version(tmp)
        installed = wheel_version(wheel, tmp)
    for label, actual in (("source tree", source), ("wheel", installed)):
        if actual != expected:
            raise SystemExit(
                f"version guard: {label} reports {actual!r}, pyproject.toml declares {expected!r}"
            )
    print(f"version guard: source tree and wheel both report {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
