"""The tool-owned ``.deepsearch-axi/manifest.json`` model and serializer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from deepsearch_axi._version import VERSION
from deepsearch_axi.failures import setup_conflict
from deepsearch_axi.fsutil import read_bytes_or_none
from deepsearch_axi.models import AGENTS, DeepsearchError, FileChange

MANIFEST_RELATIVE = ".deepsearch-axi/manifest.json"
MANIFEST_OWNER = "deepsearch-axi"
MANIFEST_SCHEMA = 1


@dataclass(frozen=True)
class ManifestHost:
    files: tuple[str, ...]
    commands: tuple[str, ...]  # hook shell strings, or plugin argv JSON for opencode


@dataclass(frozen=True)
class Manifest:
    project: str  # resolved absolute root
    executable: str  # ExecutableIdentity.path
    command: tuple[str, ...]
    hosts: dict[str, ManifestHost]  # keys in AGENTS order


@dataclass(frozen=True)
class HostPlan:
    changes: tuple[FileChange, ...]
    record: ManifestHost


def _conflict(reason: str) -> DeepsearchError:
    return DeepsearchError(setup_conflict(MANIFEST_RELATIVE, reason))


def _string_list(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def load_manifest(root: Path) -> Manifest | None:
    """Load and validate the manifest for ``root``; ``None`` when absent."""
    path = root / MANIFEST_RELATIVE
    if path.is_symlink():
        raise _conflict("manifest is a symlink")
    raw = read_bytes_or_none(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _conflict("not valid JSON") from None
    if not isinstance(data, dict):
        raise _conflict("not a JSON object")
    if data.get("owner") != MANIFEST_OWNER:
        raise _conflict("not owned by deepsearch-axi")
    if data.get("schema") != MANIFEST_SCHEMA:
        raise _conflict("unsupported schema version")
    project = data.get("project")
    if not isinstance(project, str) or project != str(root.resolve()):
        raise _conflict("project does not match this workspace")
    executable = data.get("executable")
    if not isinstance(executable, str) or not executable:
        raise _conflict("missing executable")
    command = _string_list(data.get("command"))
    if command is None or not command:
        raise _conflict("missing command")
    raw_hosts = data.get("hosts")
    if not isinstance(raw_hosts, dict):
        raise _conflict("missing hosts")
    hosts: dict[str, ManifestHost] = {}
    for agent in AGENTS:
        if agent not in raw_hosts:
            continue
        entry = raw_hosts[agent]
        if not isinstance(entry, dict):
            raise _conflict(f"invalid host entry for {agent}")
        files = _string_list(entry.get("files"))
        commands = _string_list(entry.get("commands"))
        if files is None or commands is None:
            raise _conflict(f"invalid host entry for {agent}")
        hosts[agent] = ManifestHost(files=files, commands=commands)
    return Manifest(
        project=project,
        executable=executable,
        command=command,
        hosts=hosts,
    )


def manifest_bytes(manifest: Manifest) -> bytes:
    """Serialize deterministically: AGENTS host order, two-space indent, trailing LF."""
    ordered_hosts: dict[str, ManifestHost] = {}
    for agent in AGENTS:
        if agent in manifest.hosts:
            ordered_hosts[agent] = manifest.hosts[agent]
    for agent, host in manifest.hosts.items():
        if agent not in ordered_hosts:
            ordered_hosts[agent] = host
    payload = {
        "owner": MANIFEST_OWNER,
        "schema": MANIFEST_SCHEMA,
        "version": VERSION,
        "project": manifest.project,
        "executable": manifest.executable,
        "command": list(manifest.command),
        "hosts": {
            agent: {"files": list(host.files), "commands": list(host.commands)}
            for agent, host in ordered_hosts.items()
        },
    }
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
