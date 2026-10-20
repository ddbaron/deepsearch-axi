"""Native session payload normalization and lifecycle handling.

Only ``session_id``, ``cwd`` and (for OpenCode checkpoints)
``hook_event_name`` are read from the native payload.  The raw identifier is
hashed immediately and every other field -- including ``transcript_path`` --
is ignored.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal

from deepsearch_axi.failures import invalid_event
from deepsearch_axi.manifest import load_manifest
from deepsearch_axi.models import (
    MAX_NATIVE_PAYLOAD_BYTES,
    AgentName,
    DeepsearchError,
    JsonObject,
    LocalContext,
    NativeEvent,
    SessionEventKind,
    SessionResult,
)
from deepsearch_axi.state import (
    read_view,
    record_session_event,
    scope_for,
    session_hash,
)

_PAYLOAD_LIMIT = MAX_NATIVE_PAYLOAD_BYTES
_SESSION_ID_MAX = 512
_CHECKPOINT_EVENTS = frozenset({"session.idle", "plugin.dispose"})
_EVENT_KINDS = frozenset({"start", "end"})


def read_native_payload(stream: BinaryIO | None) -> JsonObject:
    """Read one bounded JSON object from non-TTY stdin."""
    if stream is None:
        raise DeepsearchError(invalid_event("stdin is not piped"))
    try:
        if stream.isatty():
            raise DeepsearchError(invalid_event("stdin is not piped"))
    except (AttributeError, OSError, ValueError):
        pass
    try:
        raw = stream.read(_PAYLOAD_LIMIT + 1)
    except (OSError, ValueError):
        raise DeepsearchError(invalid_event("stdin could not be read")) from None
    if not isinstance(raw, (bytes, bytearray)):
        raise DeepsearchError(invalid_event("stdin is not binary"))
    data = bytes(raw)
    if len(data) > _PAYLOAD_LIMIT:
        raise DeepsearchError(invalid_event("payload exceeds 65536 bytes"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DeepsearchError(invalid_event("payload is not valid UTF-8")) from None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        raise DeepsearchError(invalid_event("payload is not valid JSON")) from None
    if not isinstance(payload, dict):
        raise DeepsearchError(invalid_event("payload is not a JSON object"))
    return payload


def normalize_event(
    event: Literal["start", "end"], agent: AgentName, root: Path, payload: JsonObject
) -> NativeEvent:
    """Validate a native payload and reduce it to a hashed, scoped event."""
    if event not in _EVENT_KINDS:
        raise DeepsearchError(invalid_event("unknown session event"))
    try:
        root_path = Path(root).resolve()
    except (OSError, RuntimeError, ValueError):
        raise DeepsearchError(invalid_event("project root could not be resolved")) from None
    if not root_path.is_dir():
        raise DeepsearchError(invalid_event("project root is not a directory"))

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise DeepsearchError(invalid_event("missing session_id"))
    if len(session_id) > _SESSION_ID_MAX:
        raise DeepsearchError(invalid_event("session_id is too long"))
    if not _safe_session_id(session_id):
        raise DeepsearchError(invalid_event("session_id contains an unsafe path"))

    cwd_value = payload.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value:
        raise DeepsearchError(invalid_event("missing cwd"))
    cwd_path = Path(cwd_value)
    if not cwd_path.is_absolute():
        raise DeepsearchError(invalid_event("cwd is not absolute"))
    try:
        cwd_resolved = cwd_path.resolve()
    except (OSError, RuntimeError, ValueError):
        raise DeepsearchError(invalid_event("cwd could not be resolved")) from None
    scope = scope_for(root_path, cwd_resolved)
    if scope is None:
        raise DeepsearchError(invalid_event("cwd is outside the project"))

    if event == "start":
        kind: SessionEventKind = "start"
    elif payload.get("hook_event_name") in _CHECKPOINT_EVENTS:
        kind = "checkpoint"
    else:
        kind = "end"
    return NativeEvent(
        session_hash=session_hash(agent, session_id),
        scope=scope,
        kind=kind,
    )


def handle_session(
    event: Literal["start", "end"],
    agent: AgentName,
    root: Path,
    native_payload: JsonObject,
    *,
    context_for: Callable[[Path], LocalContext],
) -> SessionResult:
    """Record a native event and build its document without host I/O.

    ``context_for`` is injected so this module never imports auth, network or
    installer code; ``deepsearch_axi.documents`` is imported lazily because the
    document builders live in a sibling component.
    """
    native = normalize_event(event, agent, root, native_payload)
    root_path = Path(root).resolve()
    recorded = False
    try:
        manifest = load_manifest(root_path)
    except DeepsearchError:
        manifest = None
    if manifest is not None:
        recorded = record_session_event(root_path, native, agent)

    from deepsearch_axi import documents

    if event == "start":
        cwd = Path(native_payload["cwd"]).resolve()
        context = context_for(cwd)
        prior, activity = read_view(root_path, native.scope, exclude_session=native.session_hash)
        context = replace(context, prior=prior, activity=activity)
        document = documents.ambient_document(context, agent)
    else:
        document = documents.session_end_document(native.kind, recorded)
    return SessionResult(document=document, recorded=recorded)


def _safe_session_id(session_id: str) -> bool:
    if session_id in (".", ".."):
        return False
    if "/" in session_id or "\\" in session_id or "\x00" in session_id:
        return False
    if any(ord(character) < 0x20 for character in session_id):
        return False
    try:
        session_id.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
