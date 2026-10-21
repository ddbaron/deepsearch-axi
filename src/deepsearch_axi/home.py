"""Read-only local readiness context for the home view and session start.

The assembled :class:`~deepsearch_axi.models.LocalContext` contains only
display-safe values: a collapsed executable path, the actual cwd, credential
state and source label, the validated API origin, integration states and the
prior state for the exact cwd scope. It never reads a credential value, never
opens a socket and never creates or locks tool state.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from deepsearch_axi.auth import probe_credential
from deepsearch_axi.documents import collapse_home
from deepsearch_axi.failures import local_io
from deepsearch_axi.manifest import load_manifest
from deepsearch_axi.models import (
    AGENTS,
    AgentName,
    DeepsearchError,
    IntegrationState,
    LocalContext,
)
from deepsearch_axi.origin import validate_base_url
from deepsearch_axi.state import find_state_root, read_view


def _display_path(path: Path, home: Path) -> str:
    return collapse_home(str(path), str(home))


def _running_executable(argv0: str | None) -> Path:
    """Return the absolute path of the executable that invoked this process."""
    invoked = sys.argv[0] if argv0 is None else argv0
    candidate = Path(os.path.abspath(invoked))
    if candidate.name == "deepsearch-axi" and candidate.is_file():
        return candidate
    script = Path(sys.executable).parent / "deepsearch-axi"
    if script.is_file():
        return script
    return candidate


def _manifest_states(
    root: Path | None, executable: Path
) -> tuple[tuple[AgentName, IntegrationState], ...]:
    """Read manifest-recorded integration readiness without writing anything."""
    states: dict[AgentName, IntegrationState] = {agent: "missing" for agent in AGENTS}
    if root is not None:
        try:
            manifest = load_manifest(root)
        except DeepsearchError:
            manifest = None
        if manifest is not None:
            for agent in AGENTS:
                host = manifest.hosts.get(agent)
                if host is None:
                    continue
                try:
                    present = all((root / relative).is_file() for relative in host.files)
                except OSError:
                    present = False
                if not present:
                    states[agent] = "stale"
                elif manifest.executable != str(executable):
                    states[agent] = "stale"
                else:
                    states[agent] = "installed"
    return tuple((agent, states[agent]) for agent in AGENTS)


def gather_local_context(
    env: Mapping[str, str],
    cwd: Path,
    home: Path,
    argv0: str | None = None,
    *,
    secrets: Sequence[str] = (),
) -> LocalContext:
    """Build the live, offline readiness context for ``cwd`` and ``home``."""
    cwd_path = Path(cwd)
    home_path = Path(home)
    try:
        executable = _running_executable(argv0)
    except OSError:
        executable = Path(sys.executable)

    try:
        resolved_cwd = cwd_path.resolve()
    except (OSError, RuntimeError, ValueError):
        raise DeepsearchError(local_io(".")) from None

    auth = probe_credential(env, cwd_path, home_path)
    try:
        origin = validate_base_url(env.get("DEEPSEEK_BASE_URL"))
        origin_state = "ready"
    except DeepsearchError:
        origin = "invalid"
        origin_state = "invalid"

    state_root = find_state_root(cwd_path, home_path, secrets=secrets)
    integrations = _manifest_states(state_root.root if state_root is not None else None, executable)
    prior = None
    activity = None
    if state_root is not None:
        prior, activity = read_view(state_root.root, state_root.scope)

    return LocalContext(
        bin=_display_path(executable, home_path),
        cwd=_display_path(resolved_cwd, home_path),
        auth=auth,
        origin=origin,
        origin_state=origin_state,
        integrations=integrations,
        prior=prior,
        activity=activity,
    )


__all__ = ["gather_local_context"]
