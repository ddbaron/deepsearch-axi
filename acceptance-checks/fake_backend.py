"""Loopback-only fake DeepSeek Search/Fetch backend for the offline checks.

Only the two documented native paths are implemented.  A check installs a
queue of exact responses (or faults) before invoking the product.  The server
records what actually arrived; it never records the raw Authorization header
or the API key, only whether the canary key matched.

This module is acceptance-check code.  It is never imported by the product.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"


# ---------------------------------------------------------------------------
# Fixture helpers (only documented response shapes are built here)


def documented_result(
    title,
    url,
    snippet,
    *,
    rerank_score=0.9,
    summary="",
    content="",
    site_name="example.test",
    site_icon="https://example.test/favicon.ico",
    date=None,
    authority=2,
):
    """One Search result using the complete documented field set."""
    return {
        "rerank_score": rerank_score,
        "title": title,
        "url": url,
        "snippet": snippet,
        "summary": summary,
        "content": content,
        "site_name": site_name,
        "site_icon": site_icon,
        "date": date,
        "authority": authority,
    }


def json_bytes(payload):
    # ASCII escapes keep lone surrogates representable while remaining valid
    # UTF-8 bytes on the wire.
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )


def response(payload=None, *, status=200, headers=None):
    body = b"" if payload is None else json_bytes(payload)
    return {
        "status": status,
        "body": body,
        "headers": list(headers or []),
        "fault": None,
    }


def raw_response(body, *, status=200, headers=None, fault=None):
    return {
        "status": status,
        "body": body,
        "headers": list(headers or []),
        "fault": fault,
    }


def redirect_response(status, location):
    return raw_response(
        b"",
        status=status,
        headers=[("Location", location), ("Content-Type", "text/plain")],
    )


def stall_response():
    return raw_response(b"", status=200, fault="stall")


def error_response(status, message, *, headers=None):
    # A generic documented error envelope.  The checks do not assert a
    # status-specific type/code because those are not verified by the docs.
    return response(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_request_error",
            }
        },
        status=status,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Queue entries


def search_entry(rows, *, query, count, reply=None):
    if reply is None:
        reply = response({"results": rows})
    return {
        "path": SEARCH_PATH,
        "body": {"query": query, "count": count},
        "response": reply,
    }


def fetch_entry(title, content, *, url, reply=None):
    if reply is None:
        reply = response({"title": title, "content": content})
    return {"path": FETCH_PATH, "body": {"url": url}, "response": reply}


def search_raw_entry(*, query, count, reply):
    return {
        "path": SEARCH_PATH,
        "body": {"query": query, "count": count},
        "response": reply,
    }


def fetch_raw_entry(*, url, reply):
    return {"path": FETCH_PATH, "body": {"url": url}, "response": reply}


# ---------------------------------------------------------------------------
# Loopback servers


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 - quiet by design
        return

    def _read_body(self):
        length = self.headers.get("Content-Length")
        try:
            length = int(length or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)


class _FakeHandler(_QuietHandler):
    def do_POST(self):
        self._dispatch()

    def do_GET(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def _dispatch(self):
        backend = self.server.backend
        backend._begin_request()
        try:
            self._dispatch_inner(backend)
        finally:
            backend._end_request()

    def _dispatch_inner(self, backend):
        raw_body = self._read_body()
        body = None
        body_problem = None
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                body_problem = f"request body is not UTF-8 JSON ({type(exc).__name__})"
        content_type = (self.headers.get("Content-Type") or "").split(";")[0]
        content_type_ok = content_type.strip().lower() == "application/json"
        authorization = (self.headers.get("Authorization") or "").strip()
        auth_ok = authorization.lower() == f"bearer {backend.canary}".lower()

        entry = backend._take_entry()
        if entry is None:
            backend._record(
                method=self.command,
                path=self.path,
                body=body,
                canary_matched=auth_ok,
                content_type_ok=content_type_ok,
                ok=False,
                problem="unexpected request: the scripted queue is empty",
            )
            self._send_plain(500, "unexpected request")
            return

        problem = None
        if self.command != "POST":
            problem = f"method {self.command} was not POST"
        elif self.path != entry["path"]:
            problem = f"path {self.path!r} was not {entry['path']!r}"
        elif body_problem:
            problem = body_problem
        elif not isinstance(body, dict):
            problem = "request body is not a JSON object"
        else:
            problem = _match_body(entry["path"], entry["body"], body)
        if not content_type_ok:
            problem = problem or "Content-Type was not application/json"
        if not auth_ok:
            problem = problem or "Authorization did not carry the canary key"

        backend._record(
            method=self.command,
            path=self.path,
            body=body,
            canary_matched=auth_ok,
            content_type_ok=content_type_ok,
            ok=problem is None,
            problem=problem,
        )
        if problem is not None:
            self._send_plain(400, "scripted request mismatch")
            return

        self._send_scripted(entry["response"])

    def _send_scripted(self, reply):
        fault = reply.get("fault")
        body = reply.get("body", b"")
        if fault == "stall":
            self.server.backend._release.wait(180)
            self.close_connection = True
            return
        self.send_response(reply.get("status", 200))
        for name, value in reply.get("headers", []):
            self.send_header(name, value)
        if fault == "short_body":
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            cut = min(len(body), max(1, len(body) // 2))
            self.wfile.write(body[:cut])
            try:
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return
        if not any(name.lower() == "content-length" for name, _ in reply.get("headers", [])):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def _send_plain(self, status, text):
        body = (text + "\n").encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass


def _match_body(path, expected, body):
    allowed = {"query", "count", "need_content"} if path == SEARCH_PATH else {"url"}
    extra = set(body) - allowed
    if extra:
        return f"request body has unexpected field(s): {sorted(extra)}"
    for key, want in expected.items():
        got = body.get(key, "<missing>")
        if got != want:
            return f"request body {key!r} was {got!r}, expected {want!r}"
    if path == SEARCH_PATH and body.get("need_content") not in (None, False):
        return "request body need_content was not false/omitted"
    return None


class FakeBackend:
    """One loopback listener with a scripted queue and a request ledger."""

    def __init__(self, canary):
        self.canary = canary
        self._queue = []
        self._ledger = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        self._active = 0
        self._last_activity = time.monotonic()
        self._server = _LoopbackServer(("127.0.0.1", 0), _FakeHandler)
        self._server.backend = self
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def port(self):
        return self._server.server_address[1]

    def enqueue(self, entry):
        with self._lock:
            self._queue.append(entry)

    def enqueue_many(self, entries):
        with self._lock:
            self._queue.extend(entries)

    @property
    def ledger(self):
        with self._lock:
            return [dict(item) for item in self._ledger]

    @property
    def leftover(self):
        with self._lock:
            return len(self._queue)

    def _take_entry(self):
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def _record(self, **item):
        with self._lock:
            item["index"] = len(self._ledger)
            self._ledger.append(item)
            self._last_activity = time.monotonic()

    def _begin_request(self):
        with self._lock:
            self._active += 1

    def _end_request(self):
        with self._lock:
            self._active -= 1
            self._last_activity = time.monotonic()

    def wait_quiet(self, timeout=1.0, quiet=0.08):
        """Wait briefly until no handler is active, then return.

        This is a harness barrier, not a product timeout assertion: a redirect
        follower would have sent its second request before the CLI could exit.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = self._active
                idle = time.monotonic() - self._last_activity
            if active == 0 and idle >= quiet:
                return
            time.sleep(0.01)

    def stop(self):
        self._release.set()
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


class _TargetHandler(_QuietHandler):
    def _count(self):
        target = self.server.target
        target._record(self.command, self.path)
        body = b""
        try:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass

    do_GET = _count
    do_POST = _count
    do_PUT = _count
    do_PATCH = _count
    do_DELETE = _count
    do_HEAD = _count
    do_OPTIONS = _count


class RedirectTarget:
    """A second loopback listener that counts arrivals of *any* method."""

    def __init__(self):
        self._arrivals = []
        self._lock = threading.Lock()
        self._active = 0
        self._last_activity = time.monotonic()
        self._server = _LoopbackServer(("127.0.0.1", 0), _TargetHandler)
        self._server.target = self
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def count(self):
        with self._lock:
            return len(self._arrivals)

    @property
    def arrivals(self):
        with self._lock:
            return [dict(item) for item in self._arrivals]

    def _record(self, method, path):
        with self._lock:
            self._active += 1
            self._arrivals.append({"method": method, "path": path})
        try:
            time.sleep(0)  # keep the record before the response is written
        finally:
            with self._lock:
                self._active -= 1
                self._last_activity = time.monotonic()

    def wait_quiet(self, timeout=1.0, quiet=0.08):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active = self._active
                idle = time.monotonic() - self._last_activity
            if active == 0 and idle >= quiet:
                return
            time.sleep(0.01)

    def stop(self):
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
