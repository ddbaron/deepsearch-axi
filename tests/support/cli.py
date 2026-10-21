"""Subprocess CLI helpers shared by acceptance tests.

``run_cli`` executes the real package entrypoint in an isolated child process.
When ``read_guard`` is supplied it installs a small ``sitecustomize`` shim that
records every attempted open of the watched paths before any product module is
imported, so tests can prove an absence of credential/config reads instead of
inferring it from an unrelated error.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import toon_format

_SUPPORT_ROOT = Path(__file__).resolve().parent
_REAL_SITECUSTOMIZE = _SUPPORT_ROOT / "sitecustomize" / "sitecustomize.py"

_READ_GUARD_SOURCECODE = '''\
"""Test-only audit shim: load the real sitecustomize, then log watched opens."""

import json
import os
import sys

_source = os.environ.get("DEEPSEARCH_AXI_REAL_SITECUSTOMIZE")
if _source:
    with open(_source, "r", encoding="utf-8") as _handle:
        _code = compile(_handle.read(), _source, "exec")
    exec(_code, {"__name__": "__main__", "__file__": _source})

_log = os.environ.get("DEEPSEARCH_AXI_READ_LOG")
_watched = frozenset(json.loads(os.environ.get("DEEPSEARCH_AXI_READ_WATCH", "[]")))
if _log:
    _state = {"busy": False}

    def _audit(event, args):
        if event != "open" or not args or _state["busy"]:
            return
        try:
            path = os.path.abspath(os.fspath(args[0]))
        except (TypeError, ValueError):
            return
        if path not in _watched:
            return
        _state["busy"] = True
        try:
            with open(_log, "a", encoding="utf-8") as handle:
                handle.write(path + "\\n")
        finally:
            _state["busy"] = False

    sys.addaudithook(_audit)
'''


@dataclass
class CliRun:
    code: int
    stdout: str
    stderr: str
    reads: tuple[Path, ...] = field(default_factory=tuple)


def run_cli(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    stdin: bytes | None = None,
    executable: Sequence[str] | None = None,
    timeout: float = 60,
    read_guard: Sequence[Path] = (),
) -> CliRun:
    command = (
        list(executable) if executable is not None else [sys.executable, "-m", "deepsearch_axi"]
    )
    kwargs: dict[str, Any] = {}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    environment = dict(env)
    guard_dir: Path | None = None
    log_path: Path | None = None
    if read_guard:
        guard_dir = Path(tempfile.mkdtemp(prefix="deepsearch-axi-readguard-"))
        (guard_dir / "sitecustomize.py").write_text(_READ_GUARD_SOURCECODE, encoding="utf-8")
        log_path = guard_dir / "reads.log"
        watched = [str(Path(path).resolve()) for path in read_guard]
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(guard_dir), existing) if part
        )
        environment["DEEPSEARCH_AXI_REAL_SITECUSTOMIZE"] = str(_REAL_SITECUSTOMIZE)
        environment["DEEPSEARCH_AXI_READ_LOG"] = str(log_path)
        environment["DEEPSEARCH_AXI_READ_WATCH"] = json.dumps(watched)
    try:
        completed = subprocess.run(
            command + list(args),
            cwd=str(cwd),
            env=environment,
            capture_output=True,
            timeout=timeout,
            **kwargs,
        )
        reads: tuple[Path, ...] = ()
        if log_path is not None and log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            reads = tuple(Path(line) for line in lines if line)
        return CliRun(
            code=completed.returncode,
            stdout=completed.stdout.decode("utf-8"),
            stderr=completed.stderr.decode("utf-8"),
            reads=reads,
        )
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)


def decode(text: str) -> Any:
    return toon_format.decode(text, strict=True)
