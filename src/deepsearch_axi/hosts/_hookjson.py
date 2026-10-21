"""Native JSON hook merging shared by the Claude Code and Codex planners.

The merge is deliberately conservative: only handlers whose command is
identifiably ours are removed, unrelated groups and handlers are preserved, and
a plan that produces an equal document leaves the original bytes untouched.
"""

from __future__ import annotations

import copy
import json
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from deepsearch_axi.executable import shell_command
from deepsearch_axi.failures import setup_conflict
from deepsearch_axi.fsutil import read_bytes_or_none
from deepsearch_axi.models import DeepsearchError, JsonObject

HOOK_EVENTS = ("SessionStart", "SessionEnd")
_HOOK_KINDS = {"SessionStart": "start", "SessionEnd": "end"}
_OWNED_KEYS = frozenset({"hooks", "matcher"})


def _conflict(label: str, reason: str) -> DeepsearchError:
    return DeepsearchError(setup_conflict(label, reason))


def load_json_object(path: Path, label: str) -> tuple[bytes | None, JsonObject]:
    """Read a host JSON object, returning ``(original bytes, document)``."""
    raw = read_bytes_or_none(path)
    if raw is None:
        return None, {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _conflict(label, "not valid UTF-8") from None
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise _conflict(label, "not valid JSON") from None
    if not isinstance(document, dict):
        raise _conflict(label, "not a JSON object")
    return raw, document


def desired_commands(agent: str, prefix: Sequence[str], root: Path) -> Mapping[str, str]:
    """Return the native lifecycle command for each hook event."""
    return {
        event: shell_command(
            prefix,
            ["session", kind, "--agent", agent, "--project", str(root)],
        )
        for event, kind in _HOOK_KINDS.items()
    }


def owned_predicate(
    agent: str,
    root: Path,
    recorded: Sequence[str],
    desired: Mapping[str, str],
) -> Callable[[str], bool]:
    """Return a predicate identifying handlers this integration owns."""
    known = set(recorded) | set(desired.values())

    def is_owned(command: str) -> bool:
        if command in known:
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
            and argv[6] == str(root)
        )

    return is_owned


def _handlers(group: object) -> list | None:
    if not isinstance(group, dict):
        return None
    handlers = group.get("hooks")
    return handlers if isinstance(handlers, list) else None


def count_owned(document: JsonObject, event: str, is_owned: Callable[[str], bool]) -> list[str]:
    """Return every owned handler command registered for ``event``."""
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return []
    found: list[str] = []
    for group in groups:
        handlers = _handlers(group)
        if handlers is None:
            continue
        for handler in handlers:
            if isinstance(handler, dict):
                command = handler.get("command")
                if isinstance(command, str) and is_owned(command):
                    found.append(command)
    return found


def _validate_group(group: object, event: str, label: str) -> None:
    if not isinstance(group, dict):
        return
    if "hooks" in group and not isinstance(group["hooks"], list):
        raise _conflict(label, f"hooks.{event} contains an invalid hooks list")


def merge_hooks(
    document: JsonObject,
    desired: Mapping[str, str],
    is_owned: Callable[[str], bool],
    label: str,
) -> JsonObject:
    """Return a copy of ``document`` with exactly one owned handler per event."""
    merged: JsonObject = copy.deepcopy(document)
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = {}
        merged["hooks"] = hooks
    elif not isinstance(hooks, dict):
        raise _conflict(label, "hooks is not a JSON object")
    for event in HOOK_EVENTS:
        if event in hooks:
            groups = hooks[event]
            if not isinstance(groups, list):
                raise _conflict(label, f"hooks.{event} is not a JSON array")
        else:
            groups = []
            hooks[event] = groups
        for group in groups:
            _validate_group(group, event, label)
        owned = count_owned(document, event, is_owned)
        if len(owned) == 1 and owned[0] == desired[event]:
            continue
        kept_groups: list = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = _handlers(group)
            if handlers is None:
                kept_groups.append(group)
                continue
            kept = [
                handler
                for handler in handlers
                if not (
                    isinstance(handler, dict)
                    and isinstance(handler.get("command"), str)
                    and is_owned(handler["command"])
                )
            ]
            if not kept and set(group) <= _OWNED_KEYS:
                continue
            if kept != handlers:
                group = dict(group)
                group["hooks"] = kept
            kept_groups.append(group)
        kept_groups.append({"hooks": [{"type": "command", "command": desired[event]}]})
        hooks[event] = kept_groups
    return merged


def dump_json(document: JsonObject) -> bytes:
    """Serialize a merged document deterministically."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
