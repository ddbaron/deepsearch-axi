"""Build hermetic environments for in-process and subprocess tests."""

from __future__ import annotations

import os
from pathlib import Path

FAKE_KEY = "sk-test-FAKEKEY-7f3a9c0d1e2b4a6f"

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
SITECUSTOMIZE_DIR = REPO_ROOT / "tests" / "support" / "sitecustomize"

_STRIPPED_EXACT = frozenset({"NETRC", "CODEX_HOME", "CLAUDE_CONFIG_DIR"})


def _stripped(name: str) -> bool:
    return (
        name.startswith("DEEPSEEK_")
        or name.startswith("OPENCODE_")
        or name.endswith("_PROXY")
        or name.endswith("_proxy")
        or name in _STRIPPED_EXACT
    )


def isolated_env(tmp_home: Path, **overrides: str) -> dict[str, str]:
    """Return a minimal environment: PATH plus a temp HOME/XDG tree and guard.

    Host-specific configuration, credentials, proxy and dotenv variables are
    never inherited. The sitecustomize directory comes first on PYTHONPATH so
    child Pythons install the socket guard before importing product code.
    """
    tmp_home = Path(tmp_home)
    tmp_home.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_home),
        "XDG_CONFIG_HOME": str(tmp_home / ".config"),
        "XDG_CACHE_HOME": str(tmp_home / ".cache"),
        "XDG_DATA_HOME": str(tmp_home / ".local" / "share"),
        "PYTHONPATH": os.pathsep.join((str(SITECUSTOMIZE_DIR), str(SRC_DIR))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DEEPSEARCH_AXI_NETGUARD": "1",
    }
    for name in tuple(env):
        if _stripped(name):
            del env[name]
    env.update({name: str(value) for name, value in overrides.items()})
    return env
