"""Credential resolution with a deliberately literal dotenv reader.

Precedence is the process environment, then ``./.env`` in the exact current
working directory, then ``~/.config/deepsearch-axi/.env``. Lower-priority files
are never opened once a usable value has been found. Dotenv parsing performs no
shell expansion, interpolation, or process-environment mutation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from deepsearch_axi.failures import KeyConfigReason, invalid_key_config, missing_key
from deepsearch_axi.models import (
    MAX_DOTENV_BYTES,
    Credential,
    CredentialSource,
    CredentialStatus,
    DeepsearchError,
)

_ENV_VAR = "DEEPSEEK_API_KEY"
_USER_DOTENV = Path(".config") / "deepsearch-axi" / ".env"
_UNQUOTED_COMMENT_RE = re.compile(r"\s#")


def _invalid(source: CredentialSource, reason: KeyConfigReason) -> DeepsearchError:
    error = DeepsearchError(invalid_key_config(source, reason))
    # probe_credential reads this private attribute; it never carries key material.
    error.source = source  # type: ignore[attr-defined]
    return error


def check_key(value: str, source: CredentialSource) -> str:
    """Return the stripped key, or raise ``invalid_config`` for non-printable ASCII."""
    key = value.strip()
    if any(not 0x21 <= ord(char) <= 0x7E for char in key):
        raise _invalid(source, "control_characters")
    return key


def _parse_assignment(raw: str, source: CredentialSource) -> str:
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        closing = raw.find(quote, 1)
        if closing == -1:
            raise _invalid(source, "unmatched_quote")
        remainder = raw[closing + 1 :].strip()
        if remainder and not remainder.startswith("#"):
            raise _invalid(source, "malformed")
        return raw[1:closing]
    comment = _UNQUOTED_COMMENT_RE.search(raw)
    if comment is not None:
        raw = raw[: comment.start()]
    return raw.strip()


def read_dotenv_key(path: Path, source: CredentialSource) -> str | None:
    """Read ``DEEPSEEK_API_KEY`` from a literal dotenv file.

    Missing files and blank assignments return ``None`` so the caller can fall
    through. Every other problem raises ``invalid_config`` with a fixed reason.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise _invalid(source, "unreadable") from None
    if len(data) > MAX_DOTENV_BYTES:
        raise _invalid(source, "too_large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _invalid(source, "encoding") from None
    found = False
    value: str | None = None
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        equals = stripped.find("=")
        if equals == -1:
            if stripped == _ENV_VAR:
                raise _invalid(source, "malformed")
            continue
        if stripped[:equals].strip() != _ENV_VAR:
            continue
        if found:
            raise _invalid(source, "duplicate")
        found = True
        value = _parse_assignment(stripped[equals + 1 :].strip(), source)
    if not found or value is None or not value.strip():
        return None
    return check_key(value, source)


def resolve_credential(env: Mapping[str, str], cwd: Path, home: Path) -> Credential:
    """Resolve the effective key from env, then the exact-cwd dotenv, then home."""
    env_value = env.get(_ENV_VAR)
    if env_value is not None and env_value.strip():
        return Credential(value=check_key(env_value, "env"), source="env")
    cwd_key = read_dotenv_key(Path(cwd) / ".env", "cwd_dotenv")
    if cwd_key is not None:
        return Credential(value=cwd_key, source="cwd_dotenv")
    user_key = read_dotenv_key(Path(home) / _USER_DOTENV, "user_dotenv")
    if user_key is not None:
        return Credential(value=user_key, source="user_dotenv")
    raise DeepsearchError(missing_key())


def probe_credential(env: Mapping[str, str], cwd: Path, home: Path) -> CredentialStatus:
    """Return only the readiness state and source label, never key material."""
    try:
        credential = resolve_credential(env, cwd, home)
    except DeepsearchError as error:
        if error.failure.code == "missing_key":
            return CredentialStatus(state="missing", source="none")
        if error.failure.code == "invalid_config":
            source = getattr(error, "source", "none")
            return CredentialStatus(state="invalid", source=source)
        raise
    return CredentialStatus(state="configured", source=credential.source)
