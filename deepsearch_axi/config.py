"""Environment and dotenv -> validated backend origin and credentials.

Credential precedence is the process environment, then ``./.env`` in the exact
current working directory, then ``~/.config/deepsearch-axi/.env``. Lower
priority sources are never opened once a usable value is found. Dotenv parsing
is literal: no shell expansion, interpolation, or process-environment change.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import (
    API_KEY_ENV,
    BACKEND_ENV,
    DEFAULT_BACKEND_ORIGIN,
    BackendOrigin,
    CliError,
    Settings,
    failure,
)

_BACKEND_HELP = (
    f"Unset {BACKEND_ENV} to use {DEFAULT_BACKEND_ORIGIN}, or set it to an "
    "https:// origin, or to http:// with a literal loopback IP, an optional "
    "port and no path."
)

_USER_DOTENV = Path(".config") / "deepsearch-axi" / ".env"
_MAX_DOTENV_BYTES = 65536
_UNQUOTED_COMMENT_RE = re.compile(r"\s#")

_SOURCE_LABELS = {
    "env": f"the {API_KEY_ENV} environment variable",
    "cwd_dotenv": "./.env",
    "user_dotenv": "~/.config/deepsearch-axi/.env",
}

_MISSING_MESSAGE = (
    f"No DeepSeek API key found in {API_KEY_ENV}, ./.env, or ~/.config/deepsearch-axi/.env."
)


@dataclass(frozen=True)
class Credential:
    """A resolved key. The value is deliberately excluded from repr()."""

    value: str = field(repr=False)
    source: str


@dataclass(frozen=True)
class CredentialStatus:
    state: str  # "configured" | "missing" | "invalid"
    source: str  # "env" | "cwd_dotenv" | "user_dotenv" | "unknown" | "none"


def _invalid_backend_error() -> CliError:
    return failure(
        f"{BACKEND_ENV} is set to an invalid value; it must be a bare backend origin.",
        _BACKEND_HELP,
    )


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in value)


def _has_whitespace_or_controls(value: str) -> bool:
    return any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value)


def _is_header_safe(key: str) -> bool:
    """True when the key can be sent verbatim as one Latin-1 header value."""
    if _has_control_characters(key):
        return False
    try:
        key.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def resolve_backend(environ: Mapping[str, str]) -> BackendOrigin:
    """Resolve the effective backend origin, or raise a corrective failure."""
    if BACKEND_ENV not in environ:
        return BackendOrigin(DEFAULT_BACKEND_ORIGIN, "https", "api.deepseek.com", None, False)

    raw = environ[BACKEND_ENV]
    if not raw or _has_whitespace_or_controls(raw) or "?" in raw or "#" in raw:
        raise _invalid_backend_error()

    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _invalid_backend_error() from None

    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise _invalid_backend_error()
    if not parts.netloc or "@" in parts.netloc:
        raise _invalid_backend_error()
    hostname = parts.hostname
    if not hostname:
        raise _invalid_backend_error()
    if parts.path not in ("", "/"):
        raise _invalid_backend_error()

    if scheme == "http":
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            raise _invalid_backend_error() from None
        if not literal.is_loopback:
            raise _invalid_backend_error()

    return BackendOrigin(
        origin=f"{scheme}://{parts.netloc}",
        scheme=scheme,
        host=hostname,
        port=port,
        override=True,
    )


def _invalid_key(source: str, reason: str) -> CliError:
    label = _SOURCE_LABELS.get(source, "the configured source")
    return failure(
        f"{API_KEY_ENV} in {label} is invalid ({reason}).",
        f"Fix {API_KEY_ENV} in {label}: one line "
        f"{API_KEY_ENV}=<key>, optional export/quotes, and a header-safe key.",
    )


def _validated_key(raw: str, source: str) -> str:
    key = raw.strip()
    if not _is_header_safe(key):
        raise _invalid_key(source, "characters that cannot be sent in an HTTP header")
    return key


def _parse_assignment(raw: str, source: str) -> str:
    """Parse one dotenv assignment literally, without shell expansion."""
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        closing = raw.find(quote, 1)
        if closing == -1:
            raise _invalid_key(source, "unmatched quote")
        remainder = raw[closing + 1 :].strip()
        if remainder and not remainder.startswith("#"):
            raise _invalid_key(source, "malformed")
        return raw[1:closing]
    comment = _UNQUOTED_COMMENT_RE.search(raw)
    if comment is not None:
        raw = raw[: comment.start()]
    return raw.strip()


def _read_dotenv_assignment(path: Path, source: str) -> str | None:
    """Return the raw value of one dotenv key, or None when it is absent.

    Missing files and blank assignments fall through to the next source. Every
    other problem is an invalid configuration with a fixed, value-free reason.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise _invalid_key(source, "unreadable") from None
    if len(data) > _MAX_DOTENV_BYTES:
        raise _invalid_key(source, "too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _invalid_key(source, "not UTF-8") from None

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
            if stripped == API_KEY_ENV:
                raise _invalid_key(source, "malformed")
            continue
        if stripped[:equals].strip() != API_KEY_ENV:
            continue
        if found:
            raise _invalid_key(source, "duplicate")
        found = True
        value = _parse_assignment(stripped[equals + 1 :].strip(), source)
    return value


def _read_dotenv_key(path: Path, source: str) -> str | None:
    value = _read_dotenv_assignment(path, source)
    if value is None or not value.strip():
        return None
    return _validated_key(value, source)


def resolve_credential(environ: Mapping[str, str], cwd: Path, home: Path) -> Credential:
    """Resolve the key from env, then the exact-cwd dotenv, then the user dotenv."""
    env_value = environ.get(API_KEY_ENV)
    if env_value is not None and env_value.strip():
        return Credential(_validated_key(env_value, "env"), "env")
    cwd_key = _read_dotenv_key(Path(cwd) / ".env", "cwd_dotenv")
    if cwd_key is not None:
        return Credential(cwd_key, "cwd_dotenv")
    user_key = _read_dotenv_key(Path(home) / _USER_DOTENV, "user_dotenv")
    if user_key is not None:
        return Credential(user_key, "user_dotenv")
    raise missing_credential_error()


def effective_secret(environ: Mapping[str, str], cwd: Path, home: Path) -> str | None:
    """Return the effective key for output redaction only.

    This mirrors credential precedence but deliberately returns even a key that
    header validation would reject, so no credential value can be reflected by
    an error that happens before search or fetch configuration is loaded.
    """
    env_value = environ.get(API_KEY_ENV)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    for path, source in (
        (Path(cwd) / ".env", "cwd_dotenv"),
        (Path(home) / _USER_DOTENV, "user_dotenv"),
    ):
        try:
            value = _read_dotenv_assignment(path, source)
        except CliError:
            continue
        if value is not None and value.strip():
            return value.strip()
    return None


def credential_status(environ: Mapping[str, str], cwd: Path, home: Path) -> CredentialStatus:
    """Return only the local state and source label, never key material."""
    try:
        credential = resolve_credential(environ, cwd, home)
    except CliError as error:
        if error.message == _MISSING_MESSAGE:
            return CredentialStatus("missing", "none")
        return CredentialStatus("invalid", "unknown")
    return CredentialStatus("configured", credential.source)


def missing_credential_error() -> CliError:
    return failure(
        _MISSING_MESSAGE,
        f"Set the key for this shell: export {API_KEY_ENV}=... or add "
        f"{API_KEY_ENV}=<key> to ./.env or ~/.config/deepsearch-axi/.env.",
    )


def load_settings(
    environ: Mapping[str, str], cwd: Path | None = None, home: Path | None = None
) -> Settings:
    """Resolve the backend first, then accept one stripped, header-safe key."""
    backend = resolve_backend(environ)
    credential = resolve_credential(
        environ,
        Path.cwd() if cwd is None else Path(cwd),
        Path.home() if home is None else Path(home),
    )
    return Settings(backend=backend, api_key=credential.value)
