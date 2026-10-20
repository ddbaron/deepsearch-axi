"""The single output boundary: recursive redaction, TOON encoding and stdout.

``toon_format`` is imported only here, and this module is the only product
writer of stdout (the ``_entry`` bare-version fast path is the documented
exception). The rendered string is returned exactly as encoded; no document
newline is appended.
"""

from __future__ import annotations

import os
import re
import sys

import toon_format

from deepsearch_axi.documents import error_document, redact
from deepsearch_axi.failures import internal_error
from deepsearch_axi.models import JsonObject, JsonValue

_SURROGATES = re.compile("[\ud800-\udfff]")


def _sanitize(value: JsonValue) -> JsonValue:
    """Replace lone surrogates in strings (and string keys) with U+FFFD."""
    if isinstance(value, str):
        return _SURROGATES.sub("\ufffd", value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {
            (_SURROGATES.sub("\ufffd", key) if isinstance(key, str) else key): _sanitize(item)
            for key, item in value.items()
        }
    return value


def _encode(document: JsonObject) -> str:
    return toon_format.encode(document, indent_size=2, delimiter=",")


def render_document(document: JsonObject, secrets: tuple[str, ...]) -> str:
    """Sanitize, redact and encode one document as TOON without modification."""
    prepared = redact(_sanitize(document), secrets)
    try:
        return _encode(prepared)
    except (TypeError, ValueError):
        return _encode(error_document(internal_error()))


def _redirect_stdout_to_devnull() -> None:
    """Keep a broken pipe from producing a traceback; never flush unbounded output."""
    try:
        target = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, target)
    except OSError:
        pass
    finally:
        os.close(devnull)


def write_output(text: str) -> None:
    """Write one rendered document to stdout as UTF-8; empty text writes nothing."""
    if not text:
        return
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        _redirect_stdout_to_devnull()
