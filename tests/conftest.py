"""Global pytest fixtures: offline socket guard and hermetic environment."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(REPO_ROOT), str(TESTS_DIR), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import support  # noqa: E402
from support import cli as support_cli  # noqa: E402
from support import netguard  # noqa: E402
from support.env import isolated_env  # noqa: E402
from support.stub_server import RecordedRequest, StubResponse, StubServer  # noqa: E402

# Tests may import the harness as either ``support.*`` or ``tests.support.*``;
# alias both names to the same module objects so exception classes and state
# have a single identity.
sys.modules.setdefault("tests.support", support)
for _module in (support_cli, support.env, netguard, support.stub_server):
    sys.modules.setdefault(f"tests.support.{_module.__name__.split('.')[-1]}", _module)

_STRIPPED_EXACT = frozenset({"NETRC", "CODEX_HOME", "CLAUDE_CONFIG_DIR"})


@pytest.fixture(scope="session", autouse=True)
def _install_netguard() -> Iterator[None]:
    netguard.install()
    yield


@pytest.fixture(autouse=True)
def _isolate_process_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for name in list(os.environ):
        if (
            name.startswith("DEEPSEEK_")
            or name.startswith("OPENCODE_")
            or name.endswith("_PROXY")
            or name.endswith("_proxy")
            or name in _STRIPPED_EXACT
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    yield


@pytest.fixture
def home_dir(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


@pytest.fixture
def env(home_dir: Path) -> dict[str, str]:
    return isolated_env(home_dir)


@pytest.fixture
def stub() -> Iterator[Callable[..., StubServer]]:
    with contextlib.ExitStack() as stack:

        def factory(
            responder: Callable[[RecordedRequest], StubResponse],
            ipv6: bool = False,
        ) -> StubServer:
            server = StubServer(responder, ipv6=ipv6)
            stack.enter_context(server)
            return server

        yield factory
