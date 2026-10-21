"""Resolve the running console script and the command prefix integrations record.

Setup must register a command that executes the same installation the user is
running.  A bare ``deepsearch-axi`` name is only safe when ``PATH`` resolves it
to that same file; otherwise the absolute console-script path is recorded.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from deepsearch_axi.failures import missing_executable
from deepsearch_axi.models import DeepsearchError, ExecutableIdentity

__all__ = [
    "ExecutableIdentity",
    "display_bin",
    "resolve_running_executable",
    "select_command",
    "shell_command",
]


def _executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def resolve_running_executable(
    argv0: str | None = None,
    python_executable: str | None = None,
) -> Path:
    """Return the absolute path of the console script running this process.

    ``argv0`` is the process name for a console-script invocation.  When the
    package was started as ``python -m deepsearch_axi`` there is no console
    script in ``argv0``, so the interpreter's sibling script is used instead.
    Symlinks are deliberately not resolved: the path the host invoked is the
    path the integration should keep working after relocation.
    """
    invoked = sys.argv[0] if argv0 is None else argv0
    candidate = Path(os.path.abspath(invoked))
    if candidate.name == "deepsearch-axi" and _executable_file(candidate):
        return candidate
    script = Path(python_executable or sys.executable).parent / "deepsearch-axi"
    if _executable_file(script):
        return script
    raise DeepsearchError(missing_executable())


def select_command(executable: Path, env: Mapping[str, str]) -> ExecutableIdentity:
    """Pick the bare name only when ``PATH`` resolves it to ``executable``."""
    path = Path(os.path.abspath(str(executable)))
    found = shutil.which("deepsearch-axi", path=env.get("PATH", ""))
    if found is not None:
        try:
            if os.path.samefile(found, path):
                return ExecutableIdentity(path=path, command=("deepsearch-axi",))
        except OSError:
            pass
    return ExecutableIdentity(path=path, command=(str(path),))


def shell_command(prefix: Sequence[str], args: Sequence[str]) -> str:
    """Quote a native hook command for POSIX shells."""
    return shlex.join([*prefix, *args])


def display_bin(argv0: str | None = None, home: Path | None = None) -> str:
    """Return the running executable path with the HOME prefix collapsed."""
    path = str(resolve_running_executable(argv0))
    base = str(Path.home() if home is None else Path(home)).rstrip("/")
    if base and path.startswith(base + "/"):
        return "~/" + path[len(base) + 1 :]
    if base and path == base:
        return "~"
    return path
