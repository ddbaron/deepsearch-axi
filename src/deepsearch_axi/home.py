"""Read-only local readiness context for the home view and session start.

The assembled :class:`~deepsearch_axi.models.LocalContext` contains only
non-secret fields: a collapsed executable path, the actual cwd, credential
state and source label, the validated API origin, integration states and the
prior state for the exact cwd scope. It never stores a credential value, never
opens a socket and never creates or locks tool state.

Integration readiness is inspected from the live files a setup run owns rather
than trusted from the manifest alone: a manifest record only stays
``installed`` while the recorded executable still matches and every owned
lifecycle command, Codex feature flag and generated plugin byte is intact.
Every probe failure is contained so the home view always succeeds.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

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
# Claude Code matches a hook group's matcher against the lifecycle identifier,
# the session source for SessionStart and the end reason for SessionEnd, using
# literal alternatives rather than a regular expression. The integration only
# captures every lifecycle event while the owned group's matcher covers all
# documented identifiers; a missing matcher matches all, and whitespace is
# significant (never trimmed). Claude Code also documents a "fork" SessionStart
# source that this integration does not model. Setup never writes a matcher, so
# an owned matcher naming "fork" is conservatively stale rather than trusted.
_CLAUDE_MATCHER_VALUES: dict[str, tuple[str, ...]] = {
    "SessionStart": ("startup", "resume", "clear", "compact"),
    "SessionEnd": ("clear", "resume", "logout", "prompt_input_exit", "other"),
}
# Native match-all matchers. Claude Code documents both; the setup merge writes
# no matcher at all. Codex now documents lifecycle matchers too (SessionStart
# startup|resume|clear|compact|fork, SessionEnd other), but setup never writes
# one there. Readiness therefore keeps every non-match-all Codex matcher stale:
# a host-controlled pattern is never compiled and cannot prove capture, so the
# conservative answer wins.
_MATCH_ALL_MATCHERS = frozenset({"", "*"})
_MATCHER_LENGTH_LIMIT = 256
_NO_MATCHER = object()
_HOST_FILES: dict[AgentName, tuple[str, ...]] = {
    "claude-code": (".claude/settings.json",),
    "codex": (".codex/hooks.json", ".codex/config.toml"),
    "opencode": (".opencode/plugins/deepsearch-axi.js",),
}


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


def _command_is_live(command: Sequence[str], executable: Path, env: Mapping[str, str]) -> bool:
    """Prove the manifest's command prefix resolves to the running console script.

    Setup records exactly one prefix token: either the absolute console-script
    path or the bare ``deepsearch-axi`` name that PATH resolves to that file.
    """
    if len(command) != 1:
        return False
    token = command[0]
    if not isinstance(token, str) or not token:
        return False
    has_separator = os.sep in token or (os.altsep is not None and os.altsep in token)
    if has_separator or os.path.isabs(token):
        candidate: str | None = token
    else:
        candidate = shutil.which(token, path=env.get("PATH", os.defpath))
    if candidate is None:
        return False
    return _same_executable(candidate, executable)


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


class _HookHandler(NamedTuple):
    """One native handler plus the matcher of the group that encloses it."""

    kind: object
    command: str
    matcher: object
    keys: tuple[str, ...]


def _hook_handlers(document: dict) -> dict[str, list[_HookHandler]] | None:
    """Extract every native handler, preserving its enclosing group's matcher."""
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return None
    handlers: dict[str, list[_HookHandler]] = {}
    for event in _HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            return None
        found: list[_HookHandler] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            native = group.get("hooks")
            if not isinstance(native, list):
                continue
            matcher = group["matcher"] if "matcher" in group else _NO_MATCHER
            for handler in native:
                if not isinstance(handler, dict):
                    continue
                command = handler.get("command")
                if isinstance(command, str):
                    keys = tuple(sorted(handler))
                    found.append(_HookHandler(handler.get("type"), command, matcher, keys))
        handlers[event] = found
    return handlers


def _matcher_covers(matcher: object, event: str, agent: AgentName) -> bool:
    """Validate an owned group's matcher without evaluating a host regex.

    A group without a matcher and the documented match-all forms match every
    lifecycle identifier. Claude Code additionally accepts only literal
    identifiers or pipe-separated alternations of the identifiers it documents
    for that event, and the owned group must cover every one of them. The native
    matcher treats whitespace literally, so alternatives are compared as-is: an
    alternative padded with whitespace is not a documented identifier and makes
    the group stale. Anything else, including a syntactically plausible regular
    expression, is stale: host-controlled patterns must never be compiled
    because an unverified narrowing cannot prove capture and a pathologic
    pattern could hang home.
    Codex documents lifecycle matchers too, but setup never writes one, so only
    the match-all forms are trusted there; every other matcher stays stale.
    """
    if matcher is _NO_MATCHER:
        return True
    if not isinstance(matcher, str):
        return False
    if matcher in _MATCH_ALL_MATCHERS:
        return True
    if len(matcher) > _MATCHER_LENGTH_LIMIT or agent != "claude-code":
        return False
    known = _CLAUDE_MATCHER_VALUES[event]
    tokens = matcher.split("|")
    if not all(token in known for token in tokens):
        return False
    return set(tokens) >= set(known)


def _owned_hook_command(
    command: str,
    *,
    agent: AgentName,
    project: str,
    recorded: Sequence[str],
    expected: Mapping[str, str],
) -> bool:
    """Return whether a handler command belongs to this project's integration."""
    if command in recorded or command in expected.values():
        return True
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return (
        len(argv) == 7
        and Path(argv[0]).name == "deepsearch-axi"
        and argv[1] == "session"
        and argv[2] in ("start", "end")
        and argv[3] == "--agent"
        and argv[4] == agent
        and argv[5] == "--project"
        and argv[6] == project
    )


def _expected_commands(manifest: Manifest, agent: AgentName) -> dict[str, str]:
    prefix = list(manifest.command)
    return {
        event: shlex.join(
            [*prefix, "session", kind, "--agent", agent, "--project", manifest.project]
        )
        for event, kind in zip(_HOOK_EVENTS, ("start", "end"), strict=True)
    }


def _hooks_intact(
    path: Path,
    expected: dict[str, str],
    *,
    agent: AgentName,
    project: str,
    recorded: Sequence[str],
    claude: bool,
) -> bool:
    """Require exactly one current, native command handler per lifecycle event.

    Duplicate, outdated, malformed or source-narrowed owned registrations make
    the host stale; unrelated hooks (including non-dict entries and groups with
    malformed matchers) are preserved and never counted.
    """
    document = _read_json_object(path)
    if document is None:
        return False
    if claude and document.get("disableAllHooks") is True:
        return False
    handlers = _hook_handlers(document)
    if handlers is None:
        return False
    for event in _HOOK_EVENTS:
        owned = [
            handler
            for handler in handlers[event]
            if _owned_hook_command(
                handler.command,
                agent=agent,
                project=project,
                recorded=recorded,
                expected=expected,
            )
        ]
        if len(owned) != 1:
            return False
        handler = owned[0]
        # Setup treats an owned registration as canonical only when it is exactly
        # a command handler carrying the desired command. Readiness must agree:
        # an extra field such as ``timeout`` or a missing ``type`` is stale.
        if handler.kind != "command" or handler.command != expected[event]:
            return False
        if handler.keys != ("command", "type"):
            return False
        if not _matcher_covers(handler.matcher, event, agent):
            return False
    return True


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
    # Use the setup generator so readiness compares the exact bytes it writes.
    from deepsearch_axi.hosts import opencode

    try:
        return opencode.render_plugin(command, root)
    except (OSError, ModuleNotFoundError):
        return None


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
    env: Mapping[str, str],
) -> IntegrationState:
    if not _same_executable(manifest.executable, executable):
        return "stale"
    # The manifest's command prefix is not evidence on its own: it must resolve
    # to the live console script before it can vouch for hook or plugin bytes.
    if not _command_is_live(manifest.command, executable, env):
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
        return (
            "installed"
            if _hooks_intact(
                files[0],
                expected,
                agent=agent,
                project=manifest.project,
                recorded=host.commands,
                claude=True,
            )
            else "stale"
        )
    if agent == "codex":
        if tuple(host.commands) != (expected["SessionStart"], expected["SessionEnd"]):
            return "stale"
        if not _hooks_intact(
            files[0],
            expected,
            agent=agent,
            project=manifest.project,
            recorded=host.commands,
            claude=False,
        ):
            return "stale"
        return "installed" if _codex_config_intact(files[1]) else "stale"
    if tuple(host.commands) not in {
        (),
        (json.dumps(list(manifest.command)),),
    }:
        return "stale"
    return "installed" if _plugin_intact(files[0], manifest) else "stale"


def _manifest_states(
    root: Path | None, executable: Path | None, env: Mapping[str, str]
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
            states[agent] = _host_state(root, agent, manifest, host, executable, env)
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
    except (DeepsearchError, OSError, RecursionError, ValueError):
        state_root = None
    integrations = _manifest_states(
        state_root.root if state_root is not None else None, executable, env
    )
    prior = None
    activity = None
    if state_root is not None:
        try:
            prior, activity = read_view(state_root.root, state_root.scope)
        except (DeepsearchError, OSError, RecursionError):
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
