"""One-shot, redirect-free transport for the DeepSeek tools API.

``http.client`` is deliberate: unlike ``urllib`` or third-party HTTP clients it
never reads proxy environment variables, never consults ``.netrc`` and never
follows redirects, so the bearer credential can only ever reach the validated
API origin.  A non-200 status is known before any body byte is read, which makes
"non-200 bodies are never consumed" structural rather than a convention.
"""

from __future__ import annotations

import http.client
import json
import ssl
from typing import NoReturn
from urllib.parse import urlsplit

from deepsearch_axi._version import VERSION
from deepsearch_axi.failures import (
    invalid_base_url,
    invalid_response,
    response_too_large,
    timeout_failure,
    transport_failure,
)
from deepsearch_axi.models import (
    MAX_RESPONSE_BYTES,
    TIMEOUT_SECONDS,
    TOOL_PATHS,
    Credential,
    DeepsearchError,
    HttpReply,
    JsonObject,
    SearchOptions,
    Tool,
)

_READ_CHUNK = 65536
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def build_search_body(options: SearchOptions) -> JsonObject:
    """Return the search payload in the documented key order."""
    body: JsonObject = {
        "query": options.query,
        "count": options.count,
        "need_content": options.need_content,
        "localization": options.localization,
    }
    if options.ori_question is not None:
        body["ori_question"] = options.ori_question
    return body


def build_fetch_body(url: str) -> JsonObject:
    """Return the fetch payload; the URL is never rewritten."""
    return {"url": url}


def _reject(value: str) -> NoReturn:
    """Reject ``NaN``/``Infinity`` JSON constants before validation sees them."""
    raise ValueError("non-finite JSON constant")


def _endpoint(base_url: str) -> tuple[str, str, int | None]:
    """Re-assert the already validated origin form before opening a socket."""
    try:
        parts = urlsplit(base_url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise DeepsearchError(invalid_base_url()) from None
    if (
        scheme not in {"http", "https"}
        or not host
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise DeepsearchError(invalid_base_url()) from None
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise DeepsearchError(invalid_base_url()) from None
    return scheme, host, port


def _open_connection(
    scheme: str, host: str, port: int | None, timeout_seconds: float
) -> http.client.HTTPConnection:
    if scheme == "https":
        return http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(host, port, timeout=timeout_seconds)


def _read_bounded(response: http.client.HTTPResponse, tool: Tool) -> bytes:
    """Read at most ``MAX_RESPONSE_BYTES`` plus one byte, then stop."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(_READ_CHUNK, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise DeepsearchError(response_too_large(tool))
        chunks.append(chunk)
    return b"".join(chunks)


def post_tool(
    base_url: str,
    tool: Tool,
    body: JsonObject,
    credential: Credential,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> HttpReply:
    """Perform exactly one authenticated POST and decode a 200 payload.

    Non-200 replies carry ``payload=None`` and their body is never read.
    Every dependency exception is translated to a fixed :class:`Failure` with
    ``from None`` so no raw dependency text can reach the user.
    """
    scheme, host, port = _endpoint(base_url)

    try:
        request_body = json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise DeepsearchError(invalid_response(tool)) from None

    connection: http.client.HTTPConnection | None = None
    try:
        connection = _open_connection(scheme, host, port, timeout_seconds)
        connection.request(
            "POST",
            TOOL_PATHS[tool],
            body=request_body,
            headers={
                "Authorization": f"Bearer {credential.value}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"deepsearch-axi/{VERSION}",
            },
        )
        response = connection.getresponse()

        if response.status != 200:
            # Structural guarantee: no read/readinto for non-200 replies.
            return HttpReply(status=response.status, payload=None)

        data = _read_bounded(response, tool)
        try:
            text = data.decode("utf-8")
            payload = json.loads(text, parse_constant=_reject)
        except (ValueError, RecursionError):
            # JSONDecodeError, UnicodeDecodeError and rejected constants are
            # all ValueError subclasses; deeply nested JSON raises RecursionError.
            raise DeepsearchError(invalid_response(tool)) from None
        if not isinstance(payload, dict):
            raise DeepsearchError(invalid_response(tool))
        return HttpReply(status=200, payload=payload)
    except DeepsearchError:
        raise
    except TimeoutError:
        raise DeepsearchError(timeout_failure(tool)) from None
    except (http.client.HTTPException, OSError):
        # OSError covers ssl.SSLError/CertificateError, ConnectionError,
        # socket.gaierror and every other socket-level failure.
        raise DeepsearchError(transport_failure(tool)) from None
    finally:
        if connection is not None:
            connection.close()


__all__ = [
    "build_fetch_body",
    "build_search_body",
    "post_tool",
]
