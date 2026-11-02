"""Single-request native JSON transport for the fixed DeepSeek tool endpoints.

Exactly one http.client connection and one POST: no redirects, retries,
proxies, persistence or second request. Every failure is a translated CliError
that never carries a body, header, destination, credential or exception text.
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
from collections.abc import Mapping

from .contracts import (
    OPERATION_TIMEOUT_SECONDS,
    BackendOrigin,
    CliError,
    Settings,
    failure,
    unusable_response_error,
)

_READ_CHUNK = 65536


def _timeout_error() -> CliError:
    seconds = int(OPERATION_TIMEOUT_SECONDS)
    return failure(
        f"The backend did not answer within {seconds} seconds (timeout).",
        "Rerun the command; the service may recover.",
    )


def _connection_error() -> CliError:
    return failure(
        "Could not connect to the backend: the connection failed or the network is unreachable.",
        "Check the network or DEEPSEARCH_AXI_BACKEND_URL if set, then rerun.",
    )


def _status_error(status: int) -> CliError:
    if 300 <= status <= 399:
        return failure(
            f"The backend answered with an HTTP {status} redirect, which "
            "deepsearch-axi refuses to follow.",
            "Check or unset DEEPSEARCH_AXI_BACKEND_URL, then rerun.",
        )
    if status == 400:
        return failure(
            "The backend rejected the request as invalid (HTTP 400).",
            "Correct the query, URL or limit, then rerun.",
        )
    if status == 401:
        return failure(
            "Authentication failed: the API key was rejected (HTTP 401).",
            "Check or replace the key with export DEEPSEEK_API_KEY=..., then rerun.",
        )
    if status == 402:
        return failure(
            "Insufficient balance on the DeepSeek account (HTTP 402).",
            "Top up the account, then rerun.",
        )
    if status == 403:
        return failure(
            "The backend refused the request by service policy (HTTP 403, forbidden).",
            "Use a different URL or a query the service permits, then rerun.",
        )
    if status == 404:
        return failure(
            "Not found: the service had no usable page for this request (HTTP 404).",
            "Check the URL or choose a different retrievable page, then rerun.",
        )
    if status in (408, 504):
        return failure(
            f"The service timed out before answering (HTTP {status}).",
            "Rerun the command; the service may recover.",
        )
    if status == 413:
        return failure(
            "The request was too large for the service (HTTP 413).",
            "Shorten the query or URL, then rerun.",
        )
    if status == 429:
        return failure(
            "Rate limit reached: too many requests (HTTP 429).",
            "Wait, reduce the request frequency, then rerun.",
        )
    if 500 <= status <= 599:
        return failure(
            f"The backend reported a server error (HTTP {status}).",
            "Rerun after the service recovers.",
        )
    return failure(
        f"Unexpected HTTP status {status} from the backend.",
        "Rerun; report it if it persists.",
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("operation budget exhausted")
    return remaining


def _connect(backend: BackendOrigin, deadline: float) -> http.client.HTTPConnection:
    remaining = _remaining(deadline)
    if backend.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            backend.host,
            backend.port,
            timeout=remaining,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(backend.host, backend.port, timeout=remaining)
    try:
        connection.connect()
    except BaseException:
        connection.close()
        raise
    return connection


def _extend_timeout(connection: http.client.HTTPConnection, deadline: float) -> None:
    remaining = _remaining(deadline)
    connection.timeout = remaining
    sock = connection.sock
    if sock is not None:
        sock.settimeout(remaining)


def _read_body(
    connection: http.client.HTTPConnection,
    response: http.client.HTTPResponse,
    deadline: float,
) -> bytes:
    chunks: list[bytes] = []
    while True:
        _extend_timeout(connection, deadline)
        chunk = response.read(_READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
    raw = b"".join(chunks)
    # A peer that closes before the declared Content-Length bytes arrive makes
    # read() return b"" instead of raising IncompleteRead, leaving the
    # unread length on the response. A still-truthy length is an incomplete
    # read, so a truncated body is never accepted as a complete document.
    if response.length:
        raise http.client.IncompleteRead(raw, response.length)
    return raw


def post_json(settings: Settings, path: str, payload: Mapping[str, object]) -> object:
    """POST one JSON document to ``settings.backend`` and return parsed JSON."""
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
        "Connection": "close",
    }
    connection: http.client.HTTPConnection | None = None
    raw: bytes | None = None
    try:
        connection = _connect(settings.backend, deadline)
        _extend_timeout(connection, deadline)
        connection.request("POST", path, body=body, headers=headers)
        _extend_timeout(connection, deadline)
        response = connection.getresponse()
        status = response.status
        if status != 200:
            raise _status_error(status)
        raw = _read_body(connection, response, deadline)
    except CliError:
        raise
    except TimeoutError:
        raise _timeout_error() from None
    except http.client.RemoteDisconnected:
        raise _connection_error() from None
    except OSError:
        raise _connection_error() from None
    except http.client.HTTPException:
        raise unusable_response_error() from None
    except UnicodeEncodeError:
        raise unusable_response_error() from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    if raw is None:
        raise unusable_response_error()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise unusable_response_error() from None
    try:
        return json.loads(text)
    except ValueError:
        raise unusable_response_error() from None
