"""Read-only local readiness context for the home view and session start.

The assembled :class:`~deepsearch_axi.models.LocalContext` contains only
display-safe values: a collapsed executable path, the actual cwd, credential
state and source label, the validated API origin, integration states and the
prior state for the exact cwd scope. It never reads a credential value, never
opens a socket and never creates or locks tool state.

Integration readiness is inspected from the live files a setup run owns rather
than trusted from the manifest alone: a manifest record only stays
``installed`` while the recorded executable still matches and every owned
lifecycle command, Codex feature flag and generated plugin byte is intact.
Every probe failure is contained so the home view always succeeds.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from deepsearch_axi._version import VERSION
from deepsearch_axi.auth import probe_credential
from deepsearch_axi.documents import collapse_home
from deepsearch_axi.failures import local_io
from deepsearch_axi.manifest import Manifest, ManifestHost, load_manifest
from deepsearch_axi.models import (
    AGENTS,
    AgentName,
    DeepsearchError,
    IntegrationState,
    LocalContext,
)
from deepsearch_axi.origin import validate_base_url
from deepsearch_axi.state import find_state_root, read_view

_HOOK_EVENTS = ("SessionStart", "SessionEnd")
_HOST_FILES: dict[AgentName, tuple[str, ...]] = {
    "claude-code": (".claude/settings.json",),
    "codex": (".codex/hooks.json", ".codex/config.toml"),
    "opencode": (".opencode/plugins/deepsearch-axi.js",),
}
_PLUGIN_TEMPLATE_PARTS = ("templates", "opencode_plugin.js")
_PLUGIN_COMMAND = "__DEEPSEARCH_AXI_COMMAND__"
_PLUGIN_PROJECT = "__DEEPSEARCH_AXI_PROJECT__"
_PLUGIN_VERSION = "__DEEPSEARCH_AXI_VERSION__"


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
    # ``python -m deepsearch_axi`` has no console script of its own; the
    # interpreter is the real executable path in that case.
    return Path(sys.executable)


def _same_executable(recorded: str, executable: Path) -> bool:
    if recorded == str(executable):
        return True
    try:
        return os.path.samefile(recorded, executable)
    except OSError:
        return False


def _inside_root(root: Path, relative: str) -> Path | None:
    """Resolve a manifest-owned relative path, rejecting escapes and symlinks."""
    path = Path(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        return None
    candidate = root / path
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
        candidate.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _read_json_object(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _hook_commands(document: dict) -> dict[str, set[str]] | None:
    """Extract command strings from a native Claude/Codex hooks document."""
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return None
    commands: dict[str, set[str]] = {}
    for event in _HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            return None
        found: set[str] = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    found.add(handler["command"])
        commands[event] = found
    return commands


def _expected_commands(manifest: Manifest, agent: AgentName) -> dict[str, str]:
    prefix = list(manifest.command)
    return {
        event: shlex.join(
            [*prefix, "session", kind, "--agent", agent, "--project", manifest.project]
        )
        for event, kind in zip(_HOOK_EVENTS, ("start", "end"), strict=True)
    }


def _hooks_intact(path: Path, expected: dict[str, str]) -> bool:
    document = _read_json_object(path)
    if document is None:
        return False
    commands = _hook_commands(document)
    if commands is None:
        return False
    return all(expected[event] in commands[event] for event in _HOOK_EVENTS)


def _codex_config_intact(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    try:
        import tomlkit

        document = tomlkit.parse(raw.decode("utf-8"))
        features = document.get("features")
        return features is not None and features.get("hooks") is True
    except Exception:
        # Malformed or hostile host TOML is a stale integration, never a home
        # failure. The broad catch also covers tomlkit's parser exceptions.
        return False


def _plugin_bytes(command: Sequence[str], root: Path) -> bytes | None:
    try:
        template = importlib.resources.files("deepsearch_axi").joinpath(*_PLUGIN_TEMPLATE_PARTS)
        text = template.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return None
    text = text.replace(_PLUGIN_COMMAND, json.dumps(list(command)))
    text = text.replace(_PLUGIN_PROJECT, json.dumps(str(root)))
    text = text.replace(_PLUGIN_VERSION, json.dumps(VERSION))
    return text.encode("utf-8")


def _plugin_intact(path: Path, manifest: Manifest) -> bool:
    expected = _plugin_bytes(manifest.command, Path(manifest.project))
    if expected is None:
        return False
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def _host_state(
    root: Path,
    agent: AgentName,
    manifest: Manifest,
    host: ManifestHost,
    executable: Path,
) -> IntegrationState:
    if not _same_executable(manifest.executable, executable):
        return "stale"
    for relative in host.files:
        if _inside_root(root, relative) is None:
            return "stale"
    owned = [_inside_root(root, relative) for relative in _HOST_FILES[agent]]
    if any(path is None for path in owned):
        return "stale"
    files = [path for path in owned if path is not None]
    expected = _expected_commands(manifest, agent)
    if agent == "claude-code":
        if tuple(host.commands) != (expected["SessionStart"], expected["SessionEnd"]):
            return "stale"
        return "installed" if _hooks_intact(files[0], expected) else "stale"
    if agent == "codex":
        if tuple(host.commands) != (expected["SessionStart"], expected["SessionEnd"]):
            return "stale"
        if not _hooks_intact(files[0], expected):
            return "stale"
        return "installed" if _codex_config_intact(files[1]) else "stale"
    if tuple(host.commands) not in {
        (),
        (json.dumps(list(manifest.command)),),
    }:
        return "stale"
    return "installed" if _plugin_intact(files[0], manifest) else "stale"


def _manifest_states(
    root: Path | None, executable: Path | None
) -> tuple[tuple[AgentName, IntegrationState], ...]:
    """Inspect live integration readiness without writing anything."""
    states: dict[AgentName, IntegrationState] = {agent: "missing" for agent in AGENTS}
    if root is None:
        return tuple((agent, states[agent]) for agent in AGENTS)
    try:
        manifest = load_manifest(root)
    except (DeepsearchError, OSError, RecursionError, ValueError):
        manifest = None
    if manifest is None:
        return tuple((agent, states[agent]) for agent in AGENTS)
    for agent in AGENTS:
        host = manifest.hosts.get(agent)
        if host is None:
            continue
        if executable is None:
            # Identity resolution failed: a recorded integration cannot be
            # proven installed, so it is reported stale rather than trusted.
            states[agent] = "stale"
            continue
        try:
            states[agent] = _host_state(root, agent, manifest, host, executable)
        except Exception:
            # A malformed or unreadable host record can never break readiness.
            states[agent] = "stale"
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
        executable: Path | None = _running_executable(argv0)
    except OSError:
        executable = None

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

    try:
        state_root = find_state_root(cwd_path, home_path, secrets=secrets)
    except (DeepsearchError, OSError, ValueError):
        state_root = None
    integrations = _manifest_states(state_root.root if state_root is not None else None, executable)
    prior = None
    activity = None
    if state_root is not None:
        try:
            prior, activity = read_view(state_root.root, state_root.scope)
        except (DeepsearchError, OSError):
            prior, activity = None, None

    display_executable = executable if executable is not None else Path(sys.executable)
    return LocalContext(
        bin=_display_path(display_executable, home_path),
        cwd=_display_path(resolved_cwd, home_path),
        auth=auth,
        origin=origin,
        origin_state=origin_state,
        integrations=integrations,
        prior=prior,
        activity=activity,
    )


__all__ = ["gather_local_context"]
