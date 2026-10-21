"""Transactional project setup for Claude Code, Codex and OpenCode.

Every read and validation happens while building a plan; only after all selected
hosts plan successfully are files written, in order, under an advisory lock.
An identical rerun performs no writes at all so bytes and mtimes stay stable.
"""

from __future__ import annotations

import os
from pathlib import Path

from deepsearch_axi import fsutil
from deepsearch_axi.failures import local_io
from deepsearch_axi.hosts import claude, codex, opencode
from deepsearch_axi.manifest import (
    MANIFEST_RELATIVE,
    HostPlan,
    Manifest,
    ManifestHost,
    load_manifest,
    manifest_bytes,
)
from deepsearch_axi.models import (
    AGENTS,
    AgentName,
    AgentSelection,
    DeepsearchError,
    ExecutableIdentity,
    FileChange,
    HostOutcome,
    IntegrationState,
    SetupResult,
)

_GITIGNORE_RELATIVE = ".deepsearch-axi/.gitignore"
_GITIGNORE = b"*\n"
_HANDLERS: dict[AgentName, object] = {
    "claude-code": claude,
    "codex": codex,
    "opencode": opencode,
}


def _selected(agent: AgentSelection) -> tuple[AgentName, ...]:
    return AGENTS if agent == "all" else (agent,)


def _plan(
    root: Path,
    identity: ExecutableIdentity,
    agent: AgentName,
    previous: ManifestHost | None,
) -> HostPlan:
    handler = _HANDLERS[agent]
    return handler.plan(root, identity.command, previous)  # type: ignore[attr-defined]


def _status(
    previous: ManifestHost | None,
    plan: HostPlan,
) -> str:
    if previous is None:
        return "installed"
    if not plan.changes and previous == plan.record:
        return "unchanged"
    return "repaired"


def setup_project(
    root: Path,
    agent: AgentSelection,
    executable: ExecutableIdentity,
) -> SetupResult:
    """Install or repair the selected project integrations."""
    try:
        resolved = Path(root).resolve()
    except (OSError, RuntimeError, ValueError):
        raise DeepsearchError(local_io(".")) from None
    if not resolved.is_dir():
        raise DeepsearchError(local_io("."))

    prior = load_manifest(resolved)
    selected = _selected(agent)

    plans: dict[AgentName, HostPlan] = {}
    for name in selected:
        previous = prior.hosts.get(name) if prior is not None else None
        plans[name] = _plan(resolved, executable, name, previous)

    hosts = dict(prior.hosts) if prior is not None else {}
    for name in selected:
        hosts[name] = plans[name].record
    manifest = Manifest(
        project=str(resolved),
        executable=str(executable.path),
        command=tuple(executable.command),
        hosts=hosts,
    )

    changes: list[FileChange] = []
    for name in selected:
        changes.extend(plans[name].changes)

    manifest_path = fsutil.ensure_within_root(resolved, MANIFEST_RELATIVE)
    manifest_before = fsutil.read_bytes_or_none(manifest_path)
    manifest_after = manifest_bytes(manifest)
    if manifest_after != manifest_before:
        changes.append(FileChange(path=manifest_path, before=manifest_before, after=manifest_after))

    ignore_path = fsutil.ensure_within_root(resolved, _GITIGNORE_RELATIVE)
    ignore_before = fsutil.read_bytes_or_none(ignore_path)
    if ignore_before != _GITIGNORE:
        changes.append(FileChange(path=ignore_path, before=ignore_before, after=_GITIGNORE))

    outcomes = tuple(
        HostOutcome(
            agent=name,
            status=_status(prior.hosts.get(name) if prior is not None else None, plans[name]),
            files=plans[name].record.files,
        )
        for name in selected
    )
    if not changes:
        return SetupResult(root=resolved, outcomes=outcomes, changed=False)

    lock_path = resolved / ".deepsearch-axi" / "setup.lock"
    with fsutil.locked(lock_path):
        fsutil.apply_changes(changes)
    return SetupResult(root=resolved, outcomes=outcomes, changed=True)


def _same_file(recorded: str, executable: Path) -> bool:
    if recorded == str(executable):
        return True
    try:
        return os.path.samefile(recorded, executable)
    except OSError:
        return False


def inspect_integrations(
    root: Path | None,
    executable: ExecutableIdentity | None,
) -> tuple[tuple[AgentName, IntegrationState], ...]:
    """Inspect recorded integrations without writing anything."""
    states: dict[AgentName, IntegrationState] = {name: "missing" for name in AGENTS}
    if root is None or executable is None:
        return tuple((name, states[name]) for name in AGENTS)
    try:
        resolved = Path(root).resolve()
    except (OSError, RuntimeError, ValueError):
        return tuple((name, states[name]) for name in AGENTS)
    try:
        manifest = load_manifest(resolved)
    except (DeepsearchError, OSError, RecursionError, ValueError):
        manifest = None
    if manifest is None:
        return tuple((name, states[name]) for name in AGENTS)
    if not _same_file(manifest.executable, executable.path):
        return tuple((name, states[name]) for name in AGENTS)
    for name in AGENTS:
        previous = manifest.hosts.get(name)
        if previous is None:
            continue
        handler = _HANDLERS[name]
        try:
            states[name] = handler.inspect(resolved, manifest.command, previous)  # type: ignore[attr-defined]
        except Exception:
            states[name] = "stale"
    return tuple((name, states[name]) for name in AGENTS)


__all__ = ["inspect_integrations", "setup_project"]
