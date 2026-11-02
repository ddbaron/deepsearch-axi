#!/usr/bin/env python3
"""Executable acceptance checks for the deepsearch-axi trial build.

These end-to-end checks are human-owned.  Coding agents never edit them.  The
runner invokes the installed command only as a subprocess; it never imports
product code.

Usage:
    .checks-env/bin/python acceptance-checks/run.py --list
    .checks-env/bin/python acceptance-checks/run.py                 # offline, everything
    .checks-env/bin/python acceptance-checks/run.py --capability core
    .checks-env/bin/python acceptance-checks/run.py --capability search --capability fetch
    .checks-env/bin/python acceptance-checks/run.py --story U6
    .checks-env/bin/python acceptance-checks/run.py --tier live     # immutable acceptance.sh

The interpreter running this file must provide the maintained ``toon-format``
package.  Nothing else outside the standard library is imported.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.dont_write_bytecode = True

HARNESS_TIMEOUT = 45.0
STALL_HARNESS_TIMEOUT = 90.0
LIVE_HARNESS_TIMEOUT = 900.0
MAX_MESSAGE = 520

# Every offline invocation runs under this macOS seatbelt profile when
# sandbox-exec exists: loopback stays available for the fake backend, while
# every non-local outbound connection is denied.  The live tier is the only
# path that is allowed to reach the real service.
SANDBOX_PROFILE = (
    '(version 1)(allow default)(deny network-outbound)'
    '(allow network-outbound (remote ip "localhost:*"))'
)


def _launch_prefix():
    if sys.platform == "darwin":
        sandbox = shutil.which("sandbox-exec")
        if sandbox:
            return [sandbox, "-p", SANDBOX_PROFILE]
    return []


class Failure(AssertionError):
    """A check's expected observation did not happen."""


def _load_toon():
    try:
        import toon_format  # noqa: PLC0415 - deliberate, keeps --list independent
    except ImportError:
        print(
            "acceptance checks: the interpreter running acceptance-checks/run.py must provide the "
            "maintained toon-format package (pip install toon-format)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return toon_format


def _snapshot(root):
    manifest = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
                manifest[rel] = digest.hexdigest()
            except OSError:
                manifest[rel] = "<unreadable>"
    return manifest


def _file_has_needles(path, needles):
    overlap = max((len(n) for n in needles), default=1) - 1
    tail = b""
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                window = tail + chunk
                if any(needle in window for needle in needles):
                    return True
                tail = window[-overlap:] if overlap > 0 else b""
    except OSError:
        return False
    return False


def _walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def _strings_in_order(value):
    out = []

    def visit(item):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return out


def _keys_in_order(value):
    out = []

    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str):
                    out.append(key)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return out


class Result:
    def __init__(self, argv, returncode, stdout, stderr, timed_out, duration):
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration = duration
        self.decoded = None
        self.files_delta = []


class CoreContext:
    """Per-check temporary workspace plus the invariant helpers."""

    def __init__(self, exe, canary, root, repo_root):
        self.exe = os.path.abspath(exe)
        self.canary = canary
        self.root = root
        self.repo_root = repo_root
        self.home = os.path.join(root, "home")
        self.work = os.path.join(root, "work")
        self.tmp = os.path.join(root, "tmp")
        for path in (
            self.home,
            self.work,
            self.tmp,
            os.path.join(root, "xdg-config"),
            os.path.join(root, "xdg-cache"),
            os.path.join(root, "xdg-data"),
            os.path.join(root, "xdg-state"),
        ):
            os.makedirs(path, exist_ok=True)
        self._token = canary.split("-", 2)[2]
        self._variants = self._canary_variants()
        self._segment_needles = [
            self._token[index : index + 8]
            for index in range(0, len(self._token), 8)
        ]
        self._needles = sorted(
            {v for v in self._variants if v}, key=len, reverse=True
        )
        self._byte_needles = [n.encode("utf-8") for n in self._needles]
        self._byte_needles.extend(
            seg.encode("utf-8") for seg in self._segment_needles
        )
        self.toon = None

    # -- paths and messages -------------------------------------------------

    def path(self, name):
        return os.path.join(self.work, name)

    def require(self, condition, message):
        if not condition:
            raise Failure(message)

    def describe(self, result):
        parts = [os.path.basename(self.exe)]
        for arg in result.argv[1:]:
            if len(arg) > 42:
                arg = arg[:39] + "..."
            parts.append(shlex.quote(arg))
        return " ".join(parts)

    def redact(self, text):
        text = str(text)
        for value in sorted(self._needles, key=len, reverse=True):
            if value:
                text = text.replace(value, "<redacted>")
        for value in self._segment_needles:
            text = text.replace(value, "<redacted>")
        return text

    # -- canary -------------------------------------------------------------

    def _canary_variants(self):
        key = self.canary
        variants = {
            key,
            json.dumps(key)[1:-1],
            urllib.parse.quote(key, safe=""),
            urllib.parse.quote_plus(key),
            base64.b64encode(key.encode("utf-8")).decode("ascii"),
            f"Bearer {key}",
        }
        return variants

    def scan_text_for_secret(self, text, where):
        if not text:
            return
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        for needle in self._needles:
            if needle and needle in text:
                raise Failure(
                    f"canary credential material appeared in {where}; "
                    "expected the key never to be printed or written (P1)"
                )
        for segment in self._segment_needles:
            if segment and segment in text:
                raise Failure(
                    f"a canary key fragment appeared in {where}; "
                    "expected the key never to be printed or written (P1)"
                )

    def assert_no_secret(self, result):
        self.scan_text_for_secret(result.stdout, "stdout")
        self.scan_text_for_secret(result.stderr, "stderr")
        for rel in result.files_delta:
            path = os.path.join(self.root, rel)
            if _file_has_needles(path, self._byte_needles):
                raise Failure(
                    f"canary credential material appeared in a file the product "
                    f"wrote ({rel}); expected no key on disk (P1)"
                )

    # -- invocation ---------------------------------------------------------

    def _environment(self, key, seam):
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": self.home,
            "TMPDIR": self.tmp,
            "XDG_CONFIG_HOME": os.path.join(self.root, "xdg-config"),
            "XDG_CACHE_HOME": os.path.join(self.root, "xdg-cache"),
            "XDG_DATA_HOME": os.path.join(self.root, "xdg-data"),
            "XDG_STATE_HOME": os.path.join(self.root, "xdg-state"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
        if key is not None:
            env["DEEPSEEK_API_KEY"] = key
        if seam is not None:
            env["DEEPSEARCH_AXI_BACKEND_URL"] = seam
        return env

    def invoke(
        self,
        args,
        *,
        key=None,
        seam=None,
        timeout=HARNESS_TIMEOUT,
        expect_toon=True,
    ):
        if not os.path.isfile(self.exe) or not os.access(self.exe, os.X_OK):
            raise Failure(
                f"installed command not found or not executable at {self.exe}; "
                "build/install the product first or pass --exe"
            )
        argv = [self.exe, *args]
        launch = _launch_prefix() + argv
        before = _snapshot(self.root)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                launch,
                cwd=self.work,
                env=self._environment(key, seam),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise Failure(
                f"could not execute {self.exe} ({type(exc).__name__}); "
                "expected an installed executable"
            ) from None
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
        duration = time.monotonic() - started
        after = _snapshot(self.root)
        delta = sorted(
            (set(before) ^ set(after))
            | {name for name in before if after.get(name) != before[name]}
        )
        result = Result(
            argv, proc.returncode, out, err, timed_out, duration
        )
        result.files_delta = [name for name in delta if not self._excluded(name)]
        self.assert_no_writes(result)
        self.assert_no_secret(result)
        if not timed_out and result.returncode == 0 and expect_toon:
            result.decoded = self.decode(result)
        return result

    @staticmethod
    def _excluded(rel):
        parts = rel.split(os.sep)
        return "__pycache__" in parts or rel.endswith((".pyc", ".pyo"))

    def assert_no_writes(self, result):
        if result.files_delta:
            shown = ", ".join(result.files_delta[:6])
            raise Failure(
                f"product wrote or changed file(s) in its isolated working "
                f"directory ({shown}); expected no persistence (P5)"
            )

    def assert_no_trace(self, result):
        combined = result.stdout.decode("utf-8", "replace") + "\n" + result.stderr.decode(
            "utf-8", "replace"
        )
        if "Traceback (most recent call last)" in combined or re.search(
            r'(?m)^\s*File "[^"]+", line \d+', combined
        ):
            raise Failure(
                f"{self.describe(result)} printed a stack trace; expected a "
                "plain actionable error with no traceback"
            )

    def decode(self, result):
        if not result.stdout.strip():
            raise Failure(
                f"{self.describe(result)} produced empty stdout; expected one "
                "agent-readable TOON document"
            )
        self.toon = self.toon or _load_toon()
        try:
            return self.toon.loads(result.stdout, strict=True)
        except Exception as exc:  # decoder errors are data, not crashes
            detail = self.redact(str(exc))[:160]
            raise Failure(
                f"{self.describe(result)} stdout was not one strict TOON "
                f"document ({type(exc).__name__}: {detail}); expected an "
                "agent-readable TOON result (P2)"
            ) from None

    def expect_success(self, result):
        command = self.describe(result)
        if result.timed_out:
            raise Failure(
                f"{command} did not finish within the harness bound; expected "
                "a successful TOON result"
            )
        if result.returncode != 0:
            raise Failure(
                f"{command} exited {result.returncode}; expected exit 0"
            )
        if result.stderr:
            raise Failure(
                f"{command} wrote to stderr; expected a quiet success on stdout"
            )
        self.assert_no_trace(result)
        decoded = result.decoded if result.decoded is not None else self.decode(result)
        return decoded

    def expect_error(
        self,
        result,
        returncode,
        *,
        needles=(),
        forbidden=(),
        help_line=True,
    ):
        command = self.describe(result)
        if result.timed_out:
            raise Failure(
                f"{command} did not finish within the harness bound; expected "
                f"exit {returncode} with a clear error"
            )
        if result.returncode != returncode:
            raise Failure(
                f"{command} exited {result.returncode}; expected exit {returncode}"
            )
        if result.stderr:
            raise Failure(
                f"{command} wrote to stderr; expected the error on stdout with "
                "stderr empty"
            )
        self.assert_no_trace(result)
        decoded = result.decoded if result.decoded is not None else self.decode(result)
        text = self.text(decoded) + "\n" + "\n".join(self.keys(decoded))
        lower = text.lower()
        for group in needles:
            if not any(word.lower() in lower for word in group):
                wanted = " / ".join(group)
                raise Failure(
                    f"{command} error did not mention {wanted}; expected a plain "
                    "explanation of the cause and the fix"
                )
        for word in forbidden:
            if word.lower() in lower:
                raise Failure(
                    f"{command} error contained forbidden value {word!r}; "
                    "expected no raw server text or untrusted detail"
                )
        if help_line and not self.has_help_line(decoded):
            raise Failure(
                f"{command} error did not include a help line; expected a short "
                "corrective usage hint"
            )
        return decoded, text

    def expect_version(self, result):
        command = self.describe(result)
        if result.timed_out:
            raise Failure(f"{command} did not finish within the harness bound")
        if result.returncode != 0:
            raise Failure(
                f"{command} exited {result.returncode}; expected exit 0"
            )
        if result.stderr:
            raise Failure(f"{command} wrote to stderr; expected only the version")
        self.assert_no_trace(result)
        body = result.stdout
        if body.endswith(b"\n"):
            body = body[:-1]
        if body.endswith(b"\r"):
            body = body[:-1]
        if b"\n" in body or b"\r" in body:
            raise Failure(
                f"{command} printed more than one line; expected just the "
                "version number"
            )
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise Failure(
                f"{command} printed non-UTF-8 output; expected just the version"
            ) from None
        if not re.fullmatch(r"v?\d+(?:[._-]?\d+)*(?:[A-Za-z0-9+._-]*)?", text):
            raise Failure(
                f"{command} printed {text!r}; expected just the version number"
            )
        return text

    # -- decoded-document helpers ------------------------------------------

    def strings(self, decoded):
        return _strings_in_order(decoded)

    def keys(self, decoded):
        return _keys_in_order(decoded)

    def integers(self, decoded):
        return [
            value
            for value in _walk(decoded)
            if type(value) is int  # bool is not a count
        ]

    def text(self, decoded):
        return "\n".join(self.strings(decoded))

    def has_help_line(self, decoded):
        lower = (
            self.text(decoded) + "\n" + "\n".join(self.keys(decoded))
        ).lower()
        return "help" in lower or "usage" in lower

    def has_string(self, decoded, value):
        return value in self.strings(decoded)

    def number_shown(self, decoded, number):
        for value in self.integers(decoded):
            if value == number:
                return True
        strings = self.strings(decoded)
        if number == 0 and any("zero" in value.lower() for value in strings):
            return True
        pattern = re.compile(r"(?<![\d.])" + str(number) + r"(?![\d.])")
        return any(pattern.search(value) for value in strings)

    # -- ledger -------------------------------------------------------------

    def assert_ledger(self, backend, expected):
        ledger = backend.ledger
        leftover = backend.leftover
        if leftover:
            raise Failure(
                f"scripted backend still has {leftover} unused response(s) after "
                f"{len(ledger)} request(s); expected {expected} request(s) (P5)"
            )
        if len(ledger) != expected:
            raise Failure(
                f"backend saw {len(ledger)} request(s); expected {expected} "
                "exactly (P5: no retries, follows or extra calls)"
            )
        for item in ledger:
            if not item["canary_matched"]:
                raise Failure(
                    f"request {item['index'] + 1} did not carry the canary key; "
                    "expected one authenticated native request (P1/P5)"
                )
            if not item["content_type_ok"]:
                raise Failure(
                    f"request {item['index'] + 1} did not send JSON "
                    "Content-Type; expected the documented native request"
                )
            if not item["ok"]:
                raise Failure(
                    f"request {item['index'] + 1} did not match the expected "
                    f"native request: {item.get('problem') or 'unknown mismatch'}"
                )
        self.scan_text_for_secret(json.dumps(ledger, default=str), "request ledger")
        return ledger

    def assert_target_clean(self, target, where="redirect target"):
        target.wait_quiet()
        arrivals = target.arrivals
        if arrivals:
            first = arrivals[0]
            raise Failure(
                f"{where} received {len(arrivals)} request(s), first "
                f"{first['method']} {first['path']}; expected zero requests of "
                "any method (no redirect or direct fetch)"
            )

    # -- search properties --------------------------------------------------

    def fixture_rows(self, decoded, rows):
        """Rows from the scripted answer that the decoded document shows.

        Used by the metamorphic limit invariant; it compares two documents
        rather than asserting a remembered count.
        """
        strings = self.strings(decoded)
        positions = {}
        for index, value in enumerate(strings):
            positions.setdefault(value, index)
        present = [
            row
            for row in rows
            if all(
                row[field] in positions for field in ("title", "url", "snippet")
            )
        ]
        present.sort(key=lambda row: positions[row["title"]])
        return present

    def assert_full_contains_default(self, default_doc, full_doc):
        """P6: the --full view starts with the default view of the same page."""
        default_strings = self.strings(default_doc)
        full_strings = self.strings(full_doc)
        pairs = [
            (short, long)
            for long in full_strings
            for short in default_strings
            if short and long.startswith(short)
        ]
        if not pairs:
            raise Failure(
                "the --full view did not contain the default view as a prefix "
                "for the same fake page; expected a metamorphic prefix (P6)"
            )
        short, long = max(pairs, key=lambda pair: len(pair[0]))
        if not long.startswith(short):
            raise Failure(
                "the --full view did not start with the default view; expected "
                "a metamorphic prefix (P6)"
            )
        return short, long

    def assert_search_rows(self, decoded, rows, *, context="search result"):
        strings = self.strings(decoded)
        titles = [row["title"] for row in rows]
        for row in rows:
            for field in ("title", "url", "snippet"):
                value = row[field]
                if value not in strings:
                    raise Failure(
                        f"{context}: expected returned {field} {value!r} in the "
                        "decoded document; observed it missing"
                    )
        positions = {}
        for index, value in enumerate(strings):
            positions.setdefault(value, []).append(index)
        title_positions = [positions[title][0] for title in titles]
        if title_positions != sorted(title_positions):
            raise Failure(
                f"{context}: rows appeared out of the ranked order returned by "
                "the service; expected fixture titles in order"
            )
        if len(set(title_positions)) != len(title_positions):
            raise Failure(
                f"{context}: the same title appeared in more than one ranked "
                "row; expected distinct fixture rows"
            )
        for row in rows:
            passes = sum(1 for other in rows if other["url"] == row["url"])
            if passes > 1 and strings.count(row["url"]) < passes:
                raise Failure(
                    f"{context}: duplicate returned URL {row['url']!r} was "
                    "deduplicated; expected each returned row preserved (P4)"
                )
        if not self.number_shown(decoded, len(rows)):
            raise Failure(
                f"{context}: the shown count does not equal the {len(rows)} "
                "returned row(s); expected a truthful count (P4)"
            )
        self.assert_no_invented_total(decoded, len(rows))
        return strings

    def assert_no_invented_total(self, decoded, returned):
        for value in self.integers(decoded):
            if value > 50:
                raise Failure(
                    f"search output shows integer {value}, which cannot be a "
                    "true returned count for at most 50 results; expected no "
                    "invented total (P4)"
                )
        for value in self.strings(decoded):
            lower = value.lower()
            if "total" not in lower:
                if not re.search(r"\bof\s+\d+\b", lower):
                    continue
            numbers = [int(item) for item in re.findall(r"\d+", lower)]
            if not numbers:
                continue
            if any(word in lower for word in ("unavailable", "unknown", "n/a", "none")):
                continue
            if any(number > returned for number in numbers):
                raise Failure(
                    "search output states a total larger than the returned rows; "
                    "expected no invented total (P4)"
                )

    def assert_explicit_search_empty(self, decoded):
        text = self.text(decoded).lower()
        mentions_none = "no " in text or "0 " in text or "zero" in text
        mentions_match = "match" in text or "result" in text
        suggests_broaden = any(
            word in text
            for word in ("broad", "wider", "different", "general", "shorter", "fewer")
        )
        if not (mentions_none and mentions_match and suggests_broaden):
            raise Failure(
                "empty search output did not plainly say there were no matches "
                "and suggest broadening the query; expected a meaningful empty "
                "state, not a blank success"
            )
        if not self.number_shown(decoded, 0):
            raise Failure(
                "empty search output did not show the true count 0; expected a "
                "truthful returned count (P4)"
            )

    # -- fetch properties ---------------------------------------------------

    def assert_fetch_document(
        self,
        decoded,
        *,
        url,
        title,
        content,
        mode,
        expect_truncated=None,
    ):
        strings = self.strings(decoded)
        lower_text = "\n".join(strings).lower()
        if not any(url in value for value in strings):
            raise Failure(
                f"fetch output did not identify the requested URL {url!r}; "
                "expected the URL as client context"
            )
        if title:
            if title not in strings:
                raise Failure(
                    f"fetch output did not contain the exact page title "
                    f"{title!r}; expected the service title unchanged"
                )
        elif "untitled" in lower_text:
            raise Failure(
                "fetch output invented an 'Untitled' fallback for an empty "
                "service title; expected the empty title not to be replaced"
            )
        if content == "":
            return {"preview": "", "withheld": False, "full_present": True}
        full_present = content in strings
        prefixes = [
            value
            for value in strings
            if len(value) < len(content) and content.startswith(value)
        ]
        if mode == "full":
            if not full_present:
                raise Failure(
                    "fetch --full did not return the complete page text; "
                    "expected the exact service content"
                )
            preview = content
            withheld = False
        elif full_present:
            preview = content
            withheld = False
        else:
            if not prefixes:
                raise Failure(
                    "default fetch output contained no prefix of the page text; "
                    "expected a clean prefix of the full content (P3)"
                )
            preview = max(prefixes, key=len)
            withheld = True
        if withheld and preview == "":
            raise Failure(
                "default fetch output withheld all page text; expected a "
                "nonempty preview of a nonempty page (P3)"
            )
        if expect_truncated is True and not withheld:
            raise Failure(
                "a long page was not shortened in default mode; expected the "
                "preview to be a strict prefix of the full text (P3)"
            )
        if expect_truncated is False and withheld:
            raise Failure(
                "a short page was truncated in default mode; expected the whole "
                "short text with no truncation notice (P3)"
            )
        if not self.number_shown(decoded, len(content)):
            raise Failure(
                f"fetch output did not show the true page length "
                f"{len(content)}; expected a truthful total length (P3)"
            )
        has_notice = any("--full" in value.lower() for value in strings)
        if withheld and not has_notice:
            raise Failure(
                "truncated fetch output did not name --full; expected a "
                "continuation hint exactly when text was withheld (P3)"
            )
        if not withheld and has_notice:
            raise Failure(
                "fetch output showed a truncation hint even though no text was "
                "withheld; expected the hint only when truncated (P3)"
            )
        return {"preview": preview, "withheld": withheld, "full_present": full_present}

    def assert_explicit_fetch_empty(self, decoded):
        lower = self.text(decoded).lower()
        says_empty = any(
            phrase in lower
            for phrase in (
                "no page text",
                "no text",
                "no content",
                "empty",
                "nothing",
                "zero characters",
            )
        )
        if not says_empty:
            raise Failure(
                "empty page was not reported as empty in plain words; expected "
                "an explicit empty state in both output modes"
            )


# ---------------------------------------------------------------------------
# Selection and reporting


def _select(checks, capabilities, tier, stories, *, listing):
    selected = list(checks)
    if capabilities:
        selected = [c for c in selected if c.capability in capabilities]
    if stories:
        selected = [c for c in selected if c.story in stories]
    if tier:
        selected = [c for c in selected if c.tier == tier]
    elif not listing:
        selected = [c for c in selected if c.tier == "offline"]
    return selected


def _invariant_map(checks):
    offline = [c for c in checks if c.tier == "offline"]
    by_scenario = {c.scenario: c for c in offline}
    return {
        "P1": ("all offline", offline),
        "P2": ("all offline TOON results", [c for c in offline if c.scenario != "CORE-VERSION-1"]),
        "P3": (
            "U6/U7",
            [by_scenario[name] for name in (
                "FETCH-OK-1", "FETCH-PREVIEW-1", "FETCH-STATE-1",
                "FETCH-EMPTY-1", "FETCH-INPUT-1", "FETCH-INJECTION-1",
            ) if name in by_scenario],
        ),
        "P4": (
            "U3/U4/U5",
            [by_scenario[name] for name in (
                "SEARCH-OK-1", "SEARCH-EMPTY-1", "SEARCH-STATE-1",
                "SEARCH-INPUT-1", "SEARCH-INJECTION-1",
            ) if name in by_scenario],
        ),
        "P5": ("all offline", offline),
        "P6": (
            "U6",
            [by_scenario[name] for name in ("FETCH-PREVIEW-1", "FETCH-STATE-1") if name in by_scenario],
        ),
        "P7": ("U3", [by_scenario["SEARCH-OK-1"]] if "SEARCH-OK-1" in by_scenario else []),
        "P8": ("U3", [by_scenario["SEARCH-OK-1"]] if "SEARCH-OK-1" in by_scenario else []),
    }


def _print_list(selected):
    for check in selected:
        print(
            f"CHECK {check.story} {check.scenario} "
            f"{check.capability} {check.tier}"
        )
    print()
    for prop, (stories, members) in _invariant_map(selected).items():
        if members:
            print(
                f"INVARIANT {prop} {stories} {len(members)} checks: "
                + " ".join(c.scenario for c in members)
            )
        else:
            print(f"INVARIANT {prop} {stories} 0 selected checks")


def _short_message(ctx, text):
    text = ctx.redact(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 3] + "..."
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="acceptance-checks/run.py",
        description=(
            "Run the human-owned deepsearch-axi end-to-end acceptance checks "
            "against the installed command. Offline checks use only a loopback "
            "fake backend."
        ),
    )
    parser.add_argument(
        "--exe",
        default=".venv/bin/deepsearch-axi",
        help="installed command to invoke (default: .venv/bin/deepsearch-axi)",
    )
    parser.add_argument(
        "--capability",
        action="append",
        choices=("core", "search", "fetch"),
        help="select a capability; repeat to select several",
    )
    parser.add_argument(
        "--tier",
        choices=("offline", "live"),
        help="select a tier; default for a run is offline",
    )
    parser.add_argument(
        "--story",
        action="append",
        choices=tuple(f"U{number}" for number in range(1, 11)),
        help="select a user story; repeat to select several",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print checks and story/rule mapping without running anything",
    )
    args = parser.parse_args(argv)

    try:
        from checks import CHECKS  # noqa: PLC0415 - local sibling module
    except Exception as exc:  # pragma: no cover - broken installation
        print(f"acceptance checks: could not load checks: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    seen = set()
    for check in CHECKS:
        if check.scenario in seen:
            print(f"acceptance checks: duplicate check ID {check.scenario}", file=sys.stderr)
            return 2
        seen.add(check.scenario)

    selected = _select(
        CHECKS,
        args.capability,
        args.tier,
        args.story,
        listing=args.list,
    )
    if args.list:
        if not selected:
            print("acceptance checks: no checks match the selection", file=sys.stderr)
            return 2
        _print_list(selected)
        return 0
    if not selected:
        print("acceptance checks: no checks match the selection", file=sys.stderr)
        return 2

    try:
        _load_toon()
    except SystemExit as exc:
        return int(exc.code or 2)

    canary = "sk-axi-canary-" + secrets.token_hex(24)
    exe = args.exe
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        for check in selected:
            print(
                f"FAIL  {check.story} {check.scenario} "
                f"({check.capability}/{check.tier}): executable not found or "
                f"not executable at {exe}"
            )
        print(f"\n0 passed, {len(selected)} failed")
        return 1

    passed = 0
    failed = 0
    for check in selected:
        root = tempfile.mkdtemp(prefix="deepsearch-axi-checks-")
        ctx = CoreContext(exe, canary, root, repo_root)
        failure = None
        started = time.monotonic()
        try:
            check.fn(ctx)
        except Failure as exc:
            failure = str(exc)
        except Exception as exc:  # an acceptance-check crash must still report one line
            failure = f"acceptance check crashed: {type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(root, ignore_errors=True)
        elapsed = time.monotonic() - started
        if failure is None:
            passed += 1
            print(
                f"PASS  {check.story} {check.scenario} "
                f"({check.capability}/{check.tier}) {elapsed:.1f}s"
            )
        else:
            failed += 1
            print(
                f"FAIL  {check.story} {check.scenario} "
                f"({check.capability}/{check.tier}): {_short_message(ctx, failure)}"
            )
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
