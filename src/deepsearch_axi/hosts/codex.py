"""Codex project hooks and feature-flag planner."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from deepsearch_axi.failures import setup_conflict
from deepsearch_axi.fsutil import ensure_within_root, read_bytes_or_none
from deepsearch_axi.hosts._hookjson import (
    desired_commands,
    dump_json,
    load_json_object,
    merge_hooks,
    owned_predicate,
)
from deepsearch_axi.manifest import HostPlan, ManifestHost
from deepsearch_axi.models import DeepsearchError, FileChange, IntegrationState

FILES = (".codex/hooks.json", ".codex/config.toml")
_AGENT = "codex"
_HOOKS_LABEL = FILES[0]
_CONFIG_LABEL = FILES[1]
_FEATURES = b"[features]\nhooks = true\n"


def _config_plan(root: Path) -> tuple[Path, bytes | None, bytes]:
    """Return the config path and its (prior, merged) bytes."""
    from tomlkit import dumps, parse, table

    path = ensure_within_root(root, _CONFIG_LABEL)
    raw = read_bytes_or_none(path)
    if raw is None:
        return path, None, _FEATURES
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DeepsearchError(setup_conflict(_CONFIG_LABEL, "not valid UTF-8")) from None
    try:
        document = parse(text)
    except Exception:
        raise DeepsearchError(setup_conflict(_CONFIG_LABEL, "not valid TOML")) from None
    features = document.get("features")
    if features is None:
        features = table()
        document["features"] = features
    elif not isinstance(features, dict):
        raise DeepsearchError(setup_conflict(_CONFIG_LABEL, "features is not a table"))
    hooks = features.get("hooks")
    if hooks is True:
        return path, raw, raw
    if hooks is not None and not isinstance(hooks, bool):
        raise DeepsearchError(setup_conflict(_CONFIG_LABEL, "features.hooks is not a boolean"))
    features["hooks"] = True
    try:
        after = dumps(document).encode("utf-8")
    except Exception:
        raise DeepsearchError(setup_conflict(_CONFIG_LABEL, "not valid TOML")) from None
    return path, raw, after


def plan(
    root: Path,
    prefix: Sequence[str],
    previous: ManifestHost | None,
) -> HostPlan:
    hooks_path = ensure_within_root(root, _HOOKS_LABEL)
    raw, document = load_json_object(hooks_path, _HOOKS_LABEL)
    desired = desired_commands(_AGENT, prefix, root)
    is_owned = owned_predicate(
        _AGENT, root, previous.commands if previous is not None else (), desired
    )
    merged = merge_hooks(document, desired, is_owned, _HOOKS_LABEL)
    hooks_after = raw if raw is not None and merged == document else dump_json(merged)

    config_path, config_before, config_after = _config_plan(root)

    changes: list[FileChange] = []
    if hooks_after != raw:
        changes.append(FileChange(path=hooks_path, before=raw, after=hooks_after))
    if config_after != config_before:
        changes.append(FileChange(path=config_path, before=config_before, after=config_after))
    record = ManifestHost(
        files=FILES,
        commands=(desired["SessionStart"], desired["SessionEnd"]),
    )
    return HostPlan(changes=tuple(changes), record=record)


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
