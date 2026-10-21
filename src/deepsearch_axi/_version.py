"""Single-source package version.

``pyproject.toml`` is the only place the version is written. A source checkout
reports the adjacent project version directly; an installed wheel has no
project file and reports the value recorded in its distribution metadata.
"""

from __future__ import annotations

import os

_DISTRIBUTION = "deepsearch-axi"
_SOURCE_PYPROJECT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "pyproject.toml",
)


def _project_version(path: str) -> str | None:
    """Read ``[project].version`` without importing a TOML parser.

    The fast path must not load runtime dependencies, so this deliberately
    understands only the static ``[project]`` table this repository writes.
    """
    name = None
    version = None
    section = ""
    try:
        with open(path, encoding="utf-8") as stream:
            for raw in stream:
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line
                    continue
                if section != "[project]" or not line or line.startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                if not separator:
                    continue
                key = key.strip()
                if key == "name":
                    name = value.split("#", 1)[0].strip().strip("\"'")
                elif key == "version":
                    version = value.split("#", 1)[0].strip().strip("\"'")
    except OSError:
        return None
    if name == _DISTRIBUTION and version:
        return version
    return None


def _installed_version() -> str:
    from importlib import metadata

    try:
        return metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError as error:
        raise RuntimeError(
            "deepsearch-axi version is unavailable: no adjacent pyproject.toml "
            "and no installed distribution metadata"
        ) from error


VERSION = _project_version(_SOURCE_PYPROJECT) or _installed_version()
