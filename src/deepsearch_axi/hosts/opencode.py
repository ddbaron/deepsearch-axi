"""Managed OpenCode plugin planner."""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import Sequence
from pathlib import Path

from deepsearch_axi._version import VERSION
from deepsearch_axi.failures import setup_conflict
from deepsearch_axi.fsutil import ensure_within_root, read_bytes_or_none
from deepsearch_axi.manifest import HostPlan, ManifestHost
from deepsearch_axi.models import DeepsearchError, FileChange, IntegrationState

FILES = (".opencode/plugins/deepsearch-axi.js",)
_AGENT = "opencode"
_LABEL = FILES[0]
_MARKER = b"// managed-by: deepsearch-axi"
_COMMAND = "__DEEPSEARCH_AXI_COMMAND__"
_PROJECT = "__DEEPSEARCH_AXI_PROJECT__"
_VERSION = "__DEEPSEARCH_AXI_VERSION__"
_TEMPLATE_PARTS = ("templates", "opencode_plugin.js")


def render_plugin(prefix: Sequence[str], root: Path) -> bytes:
    """Render the committed plugin template as the managed project plugin."""
    template = importlib.resources.files("deepsearch_axi").joinpath(*_TEMPLATE_PARTS)
    text = template.read_text(encoding="utf-8")
    text = text.replace(_COMMAND, json.dumps(list(prefix)))
    text = text.replace(_PROJECT, json.dumps(str(root)))
    text = text.replace(_VERSION, json.dumps(VERSION))
    return text.encode("utf-8")


def plan(
    root: Path,
    prefix: Sequence[str],
    previous: ManifestHost | None,
) -> HostPlan:
    path = ensure_within_root(root, _LABEL)
    raw = read_bytes_or_none(path)
    if raw is not None and raw.split(b"\n", 1)[0] != _MARKER:
        raise DeepsearchError(setup_conflict(_LABEL, "not a deepsearch-axi managed plugin"))
    after = render_plugin(prefix, root)
    changes = () if after == raw else (FileChange(path=path, before=raw, after=after),)
    record = ManifestHost(
        files=FILES,
        commands=(json.dumps(list(prefix)),),
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
