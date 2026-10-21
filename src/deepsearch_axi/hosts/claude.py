"""Claude Code project settings planner."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from deepsearch_axi.fsutil import ensure_within_root
from deepsearch_axi.hosts._hookjson import (
    desired_commands,
    dump_json,
    load_json_object,
    merge_hooks,
    owned_predicate,
)
from deepsearch_axi.manifest import HostPlan, ManifestHost
from deepsearch_axi.models import DeepsearchError, FileChange, IntegrationState

FILES = (".claude/settings.json",)
_AGENT = "claude-code"
_LABEL = FILES[0]


def plan(
    root: Path,
    prefix: Sequence[str],
    previous: ManifestHost | None,
) -> HostPlan:
    path = ensure_within_root(root, _LABEL)
    raw, document = load_json_object(path, _LABEL)
    desired = desired_commands(_AGENT, prefix, root)
    is_owned = owned_predicate(
        _AGENT, root, previous.commands if previous is not None else (), desired
    )
    merged = merge_hooks(document, desired, is_owned, _LABEL)
    after = raw if raw is not None and merged == document else dump_json(merged)
    changes = () if after == raw else (FileChange(path=path, before=raw, after=after),)
    record = ManifestHost(
        files=FILES,
        commands=(desired["SessionStart"], desired["SessionEnd"]),
    )
    return HostPlan(changes=changes, record=record)


def inspect(
    root: Path,
    prefix: Sequence[str],
    previous: ManifestHost | None,
) -> IntegrationState:
    if previous is None:
        return "missing"
    try:
        result = plan(root, prefix, previous)
    except (DeepsearchError, OSError, ValueError):
        return "stale"
    if result.changes:
        return "stale"
    return "installed" if tuple(previous.commands) == result.record.commands else "stale"
