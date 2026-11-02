"""The shared TOON output boundary. Writes documents to stdout only.

Every string and mapping key is redacted for the effective credential before
encoding, so usage errors, service data and generated hints share one
never-reflect-the-key boundary.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import BinaryIO

import toon_format

from .contracts import CliError

REDACTED = "[REDACTED]"


def _redact_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact(value: object, secrets: tuple[str, ...]) -> object:
    """Replace every non-empty secret occurrence in strings, including keys."""
    if not secrets:
        return value
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            (_redact_text(key, secrets) if isinstance(key, str) else key): redact(item, secrets)
            for key, item in value.items()
        }
    return value


def encode_document(document: Mapping[str, object], secrets: tuple[str, ...] = ()) -> bytes:
    """Encode one JSON-shaped document as redacted TOON plus a trailing newline."""
    text = toon_format.encode(dict(redact(document, secrets)))
    return (text + "\n").encode("utf-8", "backslashreplace")


def error_document(error: CliError) -> dict[str, object]:
    """The one error shape: a message value and a scalar corrective help value."""
    return {"error": error.message, "help": error.help_line}


def write_document(
    document: Mapping[str, object],
    stream: BinaryIO | None = None,
    secrets: tuple[str, ...] = (),
) -> None:
    target = sys.stdout.buffer if stream is None else stream
    target.write(encode_document(document, secrets))
    target.flush()


def write_version(
    version: str,
    stream: BinaryIO | None = None,
    secrets: tuple[str, ...] = (),
) -> None:
    target = sys.stdout.buffer if stream is None else stream
    target.write((_redact_text(version, secrets) + "\n").encode("utf-8"))
    target.flush()
