"""Filesystem primitives: containment, atomic writes, rollback and locking."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from deepsearch_axi.failures import local_io, setup_conflict
from deepsearch_axi.models import DeepsearchError, FileChange


def ensure_within_root(root: Path, relative: str) -> Path:
    """Return ``root / relative`` after proving it cannot escape ``root``.

    Absolute paths and ``..`` segments are rejected outright. Every existing
    component that is a symlink (including the target itself) must resolve
    inside the project root.
    """
    candidate = Path(relative)
    if candidate.is_absolute():
        raise DeepsearchError(setup_conflict(relative, "symlink or path escapes the project"))
    if any(part == ".." for part in candidate.parts):
        raise DeepsearchError(setup_conflict(relative, "symlink or path escapes the project"))
    resolved_root = root.resolve()
    parts = [part for part in candidate.parts if part not in ("", ".")]
    current = resolved_root
    for index, part in enumerate(parts):
        current = current / part
        if not current.is_symlink():
            continue
        is_target = index == len(parts) - 1
        try:
            resolved = current.resolve()
        except (OSError, RuntimeError):
            raise DeepsearchError(
                setup_conflict(relative, "symlink or path escapes the project")
            ) from None
        if is_target:
            raise DeepsearchError(setup_conflict(relative, "symlink or path escapes the project"))
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise DeepsearchError(setup_conflict(relative, "symlink or path escapes the project"))
    return resolved_root / candidate


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a same-directory temp file and replace."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".deepsearch-axi-")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o644
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _ensure_parent_dirs(parent: Path, created: list[Path]) -> None:
    missing = []
    current = parent
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        created.append(directory)


def _restore(applied: Sequence[FileChange], created_dirs: Sequence[Path]) -> None:
    for change in reversed(applied):
        try:
            if change.before is None:
                try:
                    change.path.unlink()
                except FileNotFoundError:
                    pass
            else:
                atomic_write(change.path, change.before)
        except OSError:
            pass
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError:
            pass


def apply_changes(changes: Sequence[FileChange]) -> None:
    """Apply file changes in order, restoring every prior byte on failure."""
    applied: list[FileChange] = []
    created_dirs: list[Path] = []
    for change in changes:
        try:
            _ensure_parent_dirs(change.path.parent, created_dirs)
            atomic_write(change.path, change.after)
        except OSError:
            _restore(applied, created_dirs)
            raise DeepsearchError(local_io(str(change.path))) from None
        applied.append(change)


@contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for the duration of the context.

    The lock file must be a regular file and is opened with ``O_NOFOLLOW`` so
    a planted symlink can neither redirect the lock nor make us create a file
    outside its directory. Failure labels never include the absolute path:
    the project prefix can itself be credential-bearing.
    """
    label = lock_path.name or "lock file"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DeepsearchError(local_io(label)) from None
    try:
        existing = os.lstat(lock_path)
    except FileNotFoundError:
        pass
    except OSError:
        raise DeepsearchError(local_io(label)) from None
    else:
        if not stat.S_ISREG(existing.st_mode):
            raise DeepsearchError(local_io(label))
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError:
        raise DeepsearchError(local_io(label)) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def read_bytes_or_none(path: Path) -> bytes | None:
    """Return file bytes, or ``None`` when the file does not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise DeepsearchError(local_io(str(path))) from None
