"""Subprocess CLI helpers shared by acceptance tests."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import toon_format


@dataclass
class CliRun:
    code: int
    stdout: str
    stderr: str


def run_cli(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    stdin: bytes | None = None,
    executable: Sequence[str] | None = None,
    timeout: float = 60,
) -> CliRun:
    command = (
        list(executable) if executable is not None else [sys.executable, "-m", "deepsearch_axi"]
    )
    kwargs: dict[str, Any] = {}
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    completed = subprocess.run(
        command + list(args),
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        timeout=timeout,
        **kwargs,
    )
    return CliRun(
        code=completed.returncode,
        stdout=completed.stdout.decode("utf-8"),
        stderr=completed.stderr.decode("utf-8"),
    )


def decode(text: str) -> Any:
    return toon_format.decode(text, strict=True)
