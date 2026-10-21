"""Bounded, hashed and lock-serialized workspace state.

The state file lives under the tool-owned ``.deepsearch-axi/`` directory of a
project with a valid manifest.  It stores only hashed session identities,
event kinds, opaque cwd scope identifiers and workspace counters -- never
native payloads, transcripts, queries, content or credentials.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from deepsearch_axi.failures import local_io
from deepsearch_axi.fsutil import atomic_write, locked
from deepsearch_axi.manifest import load_manifest
from deepsearch_axi.models import (
    AGENTS,
    MAX_SCOPE_RECORDS,
    MAX_SESSION_RECORDS,
    MAX_STATE_FILE_BYTES,
    Activity,
    AgentName,
    DeepsearchError,
    NativeEvent,
    PriorEvent,
    Tool,
)

STATE_RELATIVE = ".deepsearch-axi/state.json"
LOCK_RELATIVE = ".deepsearch-axi/state.lock"
STATE_SCHEMA = 1

_SCOPE_HASH_PREFIX = "h:"
_SCOPE_ESCAPE_PREFIX = "l:"
_SCOPE_OPAQUE_PREFIX = "o:"
_MAX_SCOPE_CHARS = 256
# A state file is always written at or below MAX_STATE_FILE_BYTES, but a
# larger-yet-valid file (e.g. a seed from an older/foreign writer) is still
# read within this hard bound so the next write can evict it back under the
# cap.  Anything bigger is treated as corrupt instead of being read.
_MAX_RECOVERABLE_STATE_BYTES = MAX_STATE_FILE_BYTES * 4
_MAX_WALK_LEVELS = 64
_SESSION_EVENTS = frozenset({"start", "checkpoint", "end"})
_ACTIVITY_FIELDS = ("search_ok", "search_fail", "fetch_ok", "fetch_fail")


@dataclass(frozen=True)
class StateRoot:
    root: Path  # resolved project root holding a valid manifest
    scope: str  # cwd relative to root ("." or "a/b"); "h:<32hex>" when long


def session_hash(agent: str, session_id: str) -> str:
    """Return the opaque 32-character state key for a native session id."""
    return hashlib.sha256(f"{agent}\0{session_id}".encode()).hexdigest()[:32]


def scope_for(root: Path, cwd: Path) -> str | None:
    """Return the project-relative POSIX scope for ``cwd``, or ``None`` outside."""
    try:
        root_path = Path(root).resolve()
        cwd_path = Path(cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if cwd_path == root_path:
        return "."
    try:
        relative = cwd_path.relative_to(root_path)
    except ValueError:
        return None
    scope = relative.as_posix()
    if not scope:
        return "."
    if len(scope) > _MAX_SCOPE_CHARS:
        return _SCOPE_HASH_PREFIX + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
    if scope.startswith((_SCOPE_HASH_PREFIX, _SCOPE_ESCAPE_PREFIX, _SCOPE_OPAQUE_PREFIX)):
        # Keep literal scopes out of the reserved hashed/escaped/opaque
        # namespaces so a real directory named "h:<digest>" or "o:<digest>"
        # can never alias a generated identifier.
        return _SCOPE_ESCAPE_PREFIX + scope
    return scope


def opaque_scope(scope: str, secrets: Sequence[str]) -> str:
    """Return a persisted scope identifier that never reflects a credential.

    A cwd can be named after the effective key (for example an agent launched
    from a checkout below a key-named directory).  Persisting that relative
    path verbatim would write key material into tool state, so any scope that
    contains a secret is replaced by a collision-safe, opaque digest.  Ordinary
    scopes stay readable as the architecture specifies.
    """
    for secret in secrets:
        if secret and secret in scope:
            digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
            return _SCOPE_OPAQUE_PREFIX + digest
    return scope


def find_state_root(cwd: Path, home: Path, *, secrets: Sequence[str] = ()) -> StateRoot | None:
    """Find the nearest valid tool-owned manifest at or above ``cwd``.

    The walk is read-only, checks at most 64 directories, and stops after
    ``home`` or the filesystem root.  Malformed manifests are treated as absent.
    Secret-bearing scopes are returned in their opaque persisted form.
    """
    try:
        current = Path(cwd).resolve()
        home_path = Path(home).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for _ in range(_MAX_WALK_LEVELS):
        if _manifest_ok(current):
            scope = scope_for(current, cwd)
            if scope is not None:
                return StateRoot(current, opaque_scope(scope, secrets))
        if current == home_path:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def record_session_event(root: Path, event: NativeEvent, agent: AgentName) -> bool:
    """Record a normalized native event; return ``True`` only when state changed."""
    root_path = Path(root)
    if not _manifest_ok(root_path):
        return False
    with locked(root_path / LOCK_RELATIVE):
        state = _read_state(root_path)
        sessions = state["sessions"]
        existing_index: int | None = None
        for index, record in enumerate(sessions):
            if record["id"] == event.session_hash:
                existing_index = index
                break
        if existing_index is not None:
            existing = sessions[existing_index]
            if existing["event"] == event.kind and existing["scope"] == event.scope:
                return False
        state["seq"] += 1
        if existing_index is not None:
            sessions.pop(existing_index)
        sessions.append(
            {
                "id": event.session_hash,
                "agent": agent,
                "scope": event.scope,
                "event": event.kind,
                "seq": state["seq"],
            }
        )
        data = _bounded_state_bytes(state)
        _write_state(root_path / STATE_RELATIVE, data)
    return True


def record_research(root: Path, scope: str, tool: Tool, ok: bool) -> None:
    """Increment one workspace-scoped research counter; best-effort by callers."""
    root_path = Path(root)
    if not _manifest_ok(root_path):
        return
    with locked(root_path / LOCK_RELATIVE):
        state = _read_state(root_path)
        state["seq"] += 1
        entry = state["scopes"].pop(scope, None)
        if entry is None:
            entry = {name: 0 for name in _ACTIVITY_FIELDS}
        entry[f"{tool}_{'ok' if ok else 'fail'}"] += 1
        entry["seq"] = state["seq"]
        state["scopes"][scope] = entry
        while len(state["scopes"]) > MAX_SCOPE_RECORDS:
            del state["scopes"][next(iter(state["scopes"]))]
        data = _bounded_state_bytes(state)
        _write_state(root_path / STATE_RELATIVE, data)


def read_view(
    root: Path, scope: str, exclude_session: str | None = None
) -> tuple[PriorEvent | None, Activity | None]:
    """Return exact-scope prior event and activity; never disclose siblings."""
    if not _manifest_ok(Path(root)):
        return None, None
    state = _read_state(Path(root))
    scoped = [record for record in state["sessions"] if record["scope"] == scope]
    prior: PriorEvent | None = None
    for record in reversed(scoped):
        if exclude_session is not None and record["id"] == exclude_session:
            continue
        prior = PriorEvent(
            agent=record["agent"],
            event=record["event"],
            sessions=len(scoped),
        )
        break
    entry = state["scopes"].get(scope)
    activity = None
    if entry is not None:
        activity = Activity(
            search_ok=entry["search_ok"],
            search_fail=entry["search_fail"],
            fetch_ok=entry["fetch_ok"],
            fetch_fail=entry["fetch_fail"],
        )
    return prior, activity


# ---- internals ----


def _manifest_ok(root: Path) -> bool:
    tool_dir = Path(root) / ".deepsearch-axi"
    if tool_dir.is_symlink() or not tool_dir.is_dir():
        return False
    try:
        return load_manifest(Path(root)) is not None
    except (DeepsearchError, RecursionError):
        return False


def _empty_state() -> dict:
    return {"schema": STATE_SCHEMA, "seq": 0, "sessions": [], "scopes": {}}


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_hashed_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _validated_state(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if not _is_int(schema) or schema != STATE_SCHEMA:
        return None
    seq = data.get("seq")
    if not _is_int(seq) or seq < 0:
        return None
    raw_sessions = data.get("sessions")
    raw_scopes = data.get("scopes")
    if not isinstance(raw_sessions, list) or not isinstance(raw_scopes, dict):
        return None

    sessions: list[dict] = []
    highest = seq
    for record in raw_sessions:
        if not isinstance(record, dict):
            return None
        identifier = record.get("id")
        agent = record.get("agent")
        scope = record.get("scope")
        kind = record.get("event")
        record_seq = record.get("seq")
        if not isinstance(identifier, str) or not _is_hashed_id(identifier):
            return None
        if not isinstance(agent, str) or agent not in AGENTS:
            return None
        if not isinstance(scope, str):
            return None
        if not isinstance(kind, str) or kind not in _SESSION_EVENTS:
            return None
        if not _is_int(record_seq) or record_seq < 1:
            return None
        sessions.append(
            {
                "id": identifier,
                "agent": agent,
                "scope": scope,
                "event": kind,
                "seq": record_seq,
            }
        )
        highest = max(highest, record_seq)

    scopes: dict[str, dict] = {}
    for key, entry in raw_scopes.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            return None
        counters: dict = {}
        for name in _ACTIVITY_FIELDS:
            value = entry.get(name)
            if not _is_int(value) or value < 0:
                return None
            counters[name] = value
        entry_seq = entry.get("seq")
        if not _is_int(entry_seq) or entry_seq < 1:
            return None
        counters["seq"] = entry_seq
        scopes[key] = counters
        highest = max(highest, entry_seq)

    return {"schema": STATE_SCHEMA, "seq": highest, "sessions": sessions, "scopes": scopes}


def _read_state(root: Path) -> dict:
    path = Path(root) / STATE_RELATIVE
    if path.is_symlink():
        return _empty_state()
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_RECOVERABLE_STATE_BYTES + 1)
    except FileNotFoundError:
        return _empty_state()
    except OSError:
        raise DeepsearchError(local_io(STATE_RELATIVE)) from None
    if len(raw) > _MAX_RECOVERABLE_STATE_BYTES:
        return _empty_state()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _empty_state()
    return _validated_state(data) or _empty_state()


def _encode_state(state: dict) -> bytes:
    return json.dumps(state, sort_keys=False, separators=(",", ":")).encode("utf-8")


def _evict_oldest_scope(state: dict) -> None:
    scopes = state["scopes"]
    oldest_key: str | None = None
    oldest_seq: int | None = None
    for key, entry in scopes.items():
        if oldest_seq is None or entry["seq"] < oldest_seq:
            oldest_key = key
            oldest_seq = entry["seq"]
    if oldest_key is not None:
        del scopes[oldest_key]


def _bounded_state_bytes(state: dict) -> bytes:
    """Serialize after deterministic oldest-first session then scope eviction."""
    sessions = state["sessions"]
    while len(sessions) > MAX_SESSION_RECORDS:
        sessions.pop(0)
    data = _encode_state(state)
    while len(data) > MAX_STATE_FILE_BYTES and sessions:
        sessions.pop(0)
        data = _encode_state(state)
    while len(data) > MAX_STATE_FILE_BYTES and state["scopes"]:
        _evict_oldest_scope(state)
        data = _encode_state(state)
    return data


def _write_state(path: Path, data: bytes) -> None:
    try:
        atomic_write(path, data)
    except OSError:
        raise DeepsearchError(local_io(STATE_RELATIVE)) from None
