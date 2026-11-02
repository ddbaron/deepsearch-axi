"""One check per approved draft scenario, plus the cross-cutting invariants.

Each check is tagged with its story, draft scenario ID, capability and tier.
The runner applies P1 (canary), P2 (strict TOON), and P5 (filesystem) around
every subprocess; P3 is asserted by every Fetch success and P4 by every Search
success. P6-P8 are metamorphic comparisons inside the U6/U3 checks. Search rows
are located by the fixture title, link and snippet, never by field name.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
from collections import namedtuple

from fake_backend import (
    FakeBackend,
    RedirectTarget,
    documented_result,
    error_response,
    fetch_entry,
    fetch_raw_entry,
    raw_response,
    redirect_response,
    response,
    search_entry,
    search_raw_entry,
    stall_response,
)

Check = namedtuple("Check", "scenario story capability tier fn")

ACCEPTANCE_SHA256 = "4fc430ee9ee96c083a07e210de66615f358db6dc7af70c4106331efc142a3217"
ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def _alpha(index):
    """Stable digit-free slug so count assertions cannot match a URL."""
    if index < 0:
        return "x"
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = ALPHABET[rem] + out
    return out


def _rows(prefix, count):
    return [
        documented_result(
            f"{prefix} result {_alpha(index)}",
            f"https://example.test/{prefix}/{_alpha(index)}",
            f"{prefix} snippet {_alpha(index)}",
        )
        for index in range(count)
    ]


def _preview_content(length):
    special = ["😀", "é", "e", "\u0301", '"', "\\", "\n", ","]
    chars = []
    for index in range(length):
        if 980 <= index < 1020:
            chars.append(special[index % len(special)])
        else:
            chars.append(ALPHABET[index % 26])
    return "".join(chars)


def _closed_loopback_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _has_example(text, command):
    lower = text.lower()
    if "example" in lower:
        return True
    for line in text.splitlines():
        if command in line and any(mark in line for mark in '<["\'--'):
            return True
    return False


# ---------------------------------------------------------------------------
# U1 - discover the tool


def core_version(ctx):
    with FakeBackend(ctx.canary) as backend:
        versions = []
        for alias in ("--version", "-v", "-V"):
            result = ctx.invoke([alias], seam=backend.url, expect_toon=False)
            versions.append(ctx.expect_version(result))
        ctx.require(
            len(set(versions)) == 1,
            "version aliases disagreed; expected the same installed version "
            f"from --version/-v/-V, observed {versions!r}",
        )
        ctx.assert_ledger(backend, 0)


def core_help(ctx):
    with FakeBackend(ctx.canary) as backend:
        top = ctx.expect_success(ctx.invoke(["--help"], seam=backend.url))
        top_text = ctx.text(top).lower()
        for word in (
            "search",
            "fetch",
            "doctor",
            "version",
            "deepseek_api_key",
            "deepsearch_axi_backend_url",
        ):
            ctx.require(
                word in top_text,
                f"top-level help did not mention {word!r}; expected discovery "
                "of the commands, the setup variable and the test seam",
            )
        subcommands = {
            "doctor": ("deepseek_api_key",),
            "search": ("--limit", "query"),
            "fetch": ("--full", "url"),
        }
        for command, words in subcommands.items():
            decoded = ctx.expect_success(
                ctx.invoke([command, "--help"], seam=backend.url)
            )
            text = ctx.text(decoded)
            lower = text.lower()
            ctx.require(
                command in lower,
                f"{command} --help did not name {command!r}; expected command "
                "discovery",
            )
            for word in words:
                ctx.require(
                    word in lower,
                    f"{command} --help did not mention {word!r}; expected its "
                    "arguments, flags and bounds",
                )
            ctx.require(
                _has_example(text, command),
                f"{command} --help showed no example or usage line; expected a "
                "short example so an agent can use it without documentation",
            )
        ctx.assert_ledger(backend, 0)


def core_input(ctx):
    sentinel = ctx.path("sentinel")
    cases = [
        (["definitely-not-a-command"], "definitely-not-a-command"),
        ([f"badcmd$(touch {sentinel})"], "badcmd"),
        (["--frobnicate"], "frobnicate"),
        (["doctor", "extra"], "extra"),
        (["doctor", "--full"], "full"),
        (["plugins"], "plugins"),
        (["sessions"], "sessions"),
        (["update"], "update"),
        (["setup"], "setup"),
    ]
    with FakeBackend(ctx.canary) as backend:
        for args, needle in cases:
            result = ctx.invoke(args, seam=backend.url)
            ctx.expect_error(result, 2, needles=[(needle,)], help_line=True)
        ctx.require(
            not os.path.exists(sentinel),
            "a hostile argv value executed a shell command; expected unknown "
            "input to be rejected without side effects",
        )
        ctx.assert_ledger(backend, 0)


# ---------------------------------------------------------------------------
# U2 - readiness and the single test seam


def core_ready(ctx):
    with FakeBackend(ctx.canary) as backend:
        for key, _label in ((None, "absent"), ("", "empty"), ("   ", "blank")):
            result = ctx.invoke(["doctor"], key=key, seam=backend.url)
            ctx.expect_error(
                result,
                1,
                needles=[("DEEPSEEK_API_KEY",), ("export",)],
                help_line=False,
            )
        home = ctx.expect_success(ctx.invoke([], seam=backend.url))
        home_text = ctx.text(home).lower()
        ctx.require(
            "search" in home_text and "fetch" in home_text,
            "no-argument home did not say what the tool can do; expected search "
            "and fetch to be discoverable",
        )
        ctx.require(
            len(home_text.strip()) >= 15,
            "no-argument home was too thin to identify the tool; expected a "
            "one-sentence description",
        )
        ready = ctx.expect_success(
            ctx.invoke(["doctor"], key=ctx.canary, seam=backend.url)
        )
        text = ctx.text(ready).lower()
        ctx.require(
            any(word in text for word in ("ready", "configured", "present", "ok"))
            and "local" in text
            and any(word in text for word in ("not ", "only ", "hasn't", "never"))
            and any(word in text for word in ("remote", "deepseek", "authentic", "verified")),
            "doctor with a key did not distinguish local presence from remote "
            "authentication; expected an explicit local-only readiness statement",
        )
        ctx.require(
            "127.0.0.1" in text
            and str(backend.port) in text
            and any(word in text for word in ("override", "test", "custom", "seam")),
            "doctor did not identify the effective test backend and that the "
            "override is active",
        )
        default = ctx.expect_success(
            ctx.invoke(["doctor"], key=ctx.canary, seam=None)
        )
        ctx.require(
            "api.deepseek.com" in ctx.text(default).lower(),
            "doctor did not identify the default effective backend; expected "
            "https://api.deepseek.com when the seam is unset",
        )
        ctx.assert_ledger(backend, 0)


def core_seam(ctx):
    with FakeBackend(ctx.canary) as backend:
        valid = [
            (backend.url, "127.0.0.1"),
            (backend.url + "/", "127.0.0.1"),
            ("http://[::1]:9", "::1"),
            ("https://api.deepseek.com", "api.deepseek.com"),
        ]
        for seam, needle in valid:
            decoded = ctx.expect_success(
                ctx.invoke(["doctor"], key=ctx.canary, seam=seam)
            )
            text = ctx.text(decoded)
            ctx.require(
                needle in text,
                f"doctor did not show effective origin {needle!r} for valid "
                f"seam {seam!r}",
            )
            if seam == backend.url or seam == backend.url + "/":
                ctx.require(
                    str(backend.port) in text,
                    "doctor did not show the loopback port of the effective "
                    "backend",
                )
        invalid = [
            ("", "empty"),
            ("relative/path", "relative"),
            ("file:///tmp/acceptance-checks", "file"),
            ("https://leakuser:leakpass@api.deepseek.com", "userinfo"),
            ("https://api.deepseek.com/v1/tools/search", "path"),
            ("https://api.deepseek.com?x=1", "query"),
            ("https://api.deepseek.com#frag", "fragment"),
            ("http://example.test", "https-host"),
            ("http://0.0.0.0", "unspecified"),
            ("http://192.0.2.1", "non-loopback"),
            ("http://localhost", "hostname"),
            ("http://localhost.evil.test", "lookalike"),
            ("http://127.0.0.1.example.test", "lookalike"),
        ]
        for seam, label in invalid:
            result = ctx.invoke(["doctor"], key=ctx.canary, seam=seam)
            _, text = ctx.expect_error(
                result,
                1,
                needles=[
                    ("DEEPSEARCH_AXI_BACKEND_URL",),
                    ("unset", "https", "loopback", "http"),
                ],
                forbidden=["leakuser", "leakpass"],
                help_line=False,
            )
            ctx.require(
                "leakuser" not in text.lower() and "leakpass" not in text.lower(),
                f"invalid seam ({label}) reflected raw userinfo; expected the "
                "error to avoid disclosing credentials in the address",
            )
        flag = ctx.invoke(
            ["doctor", "--backend-url", "http://127.0.0.1:9"],
            key=ctx.canary,
            seam=None,
        )
        ctx.expect_error(
            flag,
            2,
            needles=[("backend",)],
            help_line=True,
        )
        ctx.assert_ledger(backend, 0)


# ---------------------------------------------------------------------------
# U3 - search success and repeatability


def search_ok(ctx):
    three = [
        documented_result(
            "Python packaging guide",
            "https://example.test/packaging",
            "Package and distribute Python code.",
        ),
        documented_result(
            "Packaging tutorial",
            "https://example.test/packaging",
            "A second result with the same URL.",
        ),
        documented_result(
            "Python build tools",
            "https://example.test/builds",
            "Build wheels and source distributions.",
        ),
    ]
    two = [
        documented_result(
            "Limit two first",
            "https://example.test/two/a",
            "First exactly-limit row.",
        ),
        documented_result(
            "Limit two second",
            "https://example.test/two/b",
            "Second exactly-limit row.",
        ),
    ]
    fifty = _rows("fifty", 50)
    query = "python packaging"
    cases = [
        ([], 10, three),
        (["--limit", "1"], 1, three),
        (["--limit", "2"], 2, two),
        (["--limit", "49"], 49, three),
        (["--limit", "50"], 50, fifty),
    ]
    with FakeBackend(ctx.canary) as backend:
        for args, count, rows in cases:
            backend.enqueue(search_entry(rows, query=query, count=count))
        for args, count, rows in cases:
            result = ctx.invoke(["search", query, *args], key=ctx.canary, seam=backend.url)
            decoded = ctx.expect_success(result)
            emitted = rows[: min(count, len(rows))]
            ctx.assert_search_rows(decoded, emitted, context=f"search {args or 'default'}")
            text = ctx.text(decoded).lower()
            excluded = [row["title"] for row in rows[len(emitted) :]]
            for title in excluded:
                ctx.require(
                    title not in ctx.strings(decoded),
                    f"search with limit {count} emitted row {title!r} beyond "
                    "the limit; expected at most the requested count (P4)",
                )
            if len(emitted) == count and count < 50:
                ctx.require(
                    "limit" in text
                    and any(
                        word in text
                        for word in ("more", "50", "additional", "next", "higher", "increase")
                    ),
                    f"search returned exactly limit {count} without a hint to "
                    "ask for more; expected a short continuation hint",
                )
            if count == 50:
                ctx.require(
                    not re.search(r"--limit\s*(?:5[1-9]|[6-9]\d|\d{3,})", text)
                    and "more than 50" not in text,
                    "search at the maximum limit still suggested a higher "
                    "limit; expected no guarantee of more than 50 results",
                )
        ledger = ctx.assert_ledger(backend, len(cases))
        counts = [item["body"].get("count") for item in ledger]
        ctx.require(
            counts == [10, 1, 2, 49, 50],
            f"search requests used counts {counts!r}; expected the effective "
            "limit, defaulting to 10",
        )
        for item in ledger:
            ctx.require(
                item["body"].get("query") == query,
                "search request changed the query; expected the typed query "
                "unchanged",
            )

    # P7: limit N is exactly the first N rows of limit N+1 from one answer.
    metamorphic = _rows("metamorphic", 4)
    with FakeBackend(ctx.canary) as backend:
        backend.enqueue(search_entry(metamorphic, query=query, count=2))
        backend.enqueue(search_entry(metamorphic, query=query, count=3))
        n_doc = ctx.expect_success(
            ctx.invoke(
                ["search", query, "--limit", "2"], key=ctx.canary, seam=backend.url
            )
        )
        n1_doc = ctx.expect_success(
            ctx.invoke(
                ["search", query, "--limit", "3"], key=ctx.canary, seam=backend.url
            )
        )
        ctx.assert_search_rows(n_doc, metamorphic[:2], context="limit N")
        ctx.assert_search_rows(n1_doc, metamorphic[:3], context="limit N+1")
        rows_n = ctx.fixture_rows(n_doc, metamorphic)
        rows_n1 = ctx.fixture_rows(n1_doc, metamorphic)
        ctx.require(
            rows_n == rows_n1[:2],
            "search --limit 2 did not return exactly the first two rows of the "
            "same scripted answer returned by --limit 3; expected the prefix "
            "metamorphic invariant (P7)",
        )
        ctx.assert_ledger(backend, 2)

    # P8: different unused backend fields do not change the selected output.
    selected = (
        "Metamorphic selected title",
        "https://example.test/metamorphic-extra",
        "Metamorphic selected snippet",
    )
    without_extra = documented_result(
        *selected,
        summary="",
        content="",
        site_name="example.test",
        site_icon="",
        date=None,
        authority=-1,
        rerank_score=0.0,
    )
    with_extra = documented_result(
        *selected,
        summary="UNUSED-SUMMARY-A",
        content="UNUSED-CONTENT-A",
        site_name="Extra Site",
        site_icon="https://example.test/icon-a",
        date="2026-01-01",
        authority=4,
        rerank_score=0.99,
    )
    with FakeBackend(ctx.canary) as backend:
        backend.enqueue(search_entry([without_extra], query=query, count=10))
        backend.enqueue(search_entry([with_extra], query=query, count=10))
        plain = ctx.expect_success(
            ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
        )
        enriched = ctx.expect_success(
            ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
        )
        ctx.assert_search_rows(plain, [without_extra], context="without extra fields")
        ctx.assert_search_rows(enriched, [with_extra], context="with extra fields")
        ctx.require(
            plain == enriched,
            "search output changed when the scripted answer carried different "
            "unused fields; expected identical selected results (P8)",
        )
        ctx.assert_ledger(backend, 2)


def search_empty(ctx):
    query = "unfindable phrase"
    with FakeBackend(ctx.canary) as backend:
        backend.enqueue(search_entry([], query=query, count=10))
        result = ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
        decoded = ctx.expect_success(result)
        ctx.assert_explicit_search_empty(decoded)
        text = ctx.text(decoded).lower()
        ctx.require(
            ("search" in text or "query" in text)
            and any(word in text for word in ("broad", "wider", "different", "general", "shorter")),
            "empty search did not offer a broader-query hint; expected a "
            "meaningful next step",
        )
        ctx.assert_ledger(backend, 1)


def search_state(ctx):
    query = "repeatable read"
    first = _rows("state-a", 2)
    second = _rows("state-b", 2)
    with FakeBackend(ctx.canary) as backend:
        backend.enqueue(search_entry(first, query=query, count=10))
        backend.enqueue(
            search_raw_entry(
                query=query,
                count=10,
                reply=error_response(500, "temporary failure STALE-MARKER"),
            )
        )
        backend.enqueue(search_entry(second, query=query, count=10))
        one = ctx.expect_success(
            ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
        )
        ctx.assert_search_rows(one, first, context="state first success")
        two_dec, _text_two = ctx.expect_error(
            ctx.invoke(["search", query], key=ctx.canary, seam=backend.url),
            1,
            needles=[("server", "service", "error", "failure"), ("retry", "rerun", "again", "later")],
            forbidden=["STALE-MARKER"],
            help_line=False,
        )
        ctx.require(
            not any(row["title"] in ctx.strings(two_dec) for row in first),
            "a failed search printed stale rows from the previous success",
        )
        three = ctx.expect_success(
            ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
        )
        ctx.assert_search_rows(three, second, context="state recovery success")
        ctx.require(
            not any(row["title"] in ctx.strings(three) for row in first),
            "the recovery search reused the earlier result set; expected the "
            "changed rows from the service",
        )
        ledger = ctx.assert_ledger(backend, 3)
        ctx.require(
            all(item["body"].get("query") == query for item in ledger),
            "the three state invocations did not send the same query; expected "
            "identical independent requests",
        )
        ctx.require(
            len({tuple(sorted(item["body"].items())) for item in ledger}) == 1,
            "the three state invocations sent different request bodies; "
            "expected no hidden retry or session state",
        )


# ---------------------------------------------------------------------------
# U4 - empty search


def search_credential(ctx):
    with FakeBackend(ctx.canary) as backend:
        for key, _label in ((None, "absent"), ("", "empty"), ("   ", "blank")):
            result = ctx.invoke(["search", "python packaging"], key=key, seam=backend.url)
            ctx.expect_error(
                result,
                1,
                needles=[("DEEPSEEK_API_KEY",), ("export",)],
                help_line=False,
            )
        ctx.assert_ledger(backend, 0)


# ---------------------------------------------------------------------------
# U5 - bad search input and injection


def search_input(ctx):
    with FakeBackend(ctx.canary) as backend:
        max_limit = "1" + "0" * 100
        invalid = [
            (["search"], "query"),
            (["search", ""], "query"),
            (["search", "   "], "query"),
            (["search", "a" * 129], ("128", "length", "long", "limit")),
            (["search", "a" * 10000], ("128", "length", "long", "limit")),
            (["search", "a", "b"], ("b", "argument", "positional", "unexpected", "extra")),
            (["search", "a", "--limit"], ("limit", "count")),
            (["search", "a", "--limit", "0"], ("limit", "count")),
            (["search", "a", "--limit", "51"], ("limit", "count")),
            (["search", "a", "--limit", "-1"], ("limit", "count")),
            (["search", "a", "--limit", "1.5"], ("limit", "count")),
            (["search", "a", "--limit", "true"], ("limit", "count")),
            (["search", "a", "--limit", "null"], ("limit", "count")),
            (["search", "a", "--limit", max_limit], ("limit", "count")),
            (["search", "a", "--limt", "2"], ("limt", "limit", "unexpected")),
            (["search", "a", "--full"], ("full", "unexpected")),
        ]
        for args, needles in invalid:
            result = ctx.invoke(args, seam=backend.url)
            ctx.expect_error(result, 2, needles=[needles], help_line=True)
        ctx.assert_ledger(backend, 0)

        valid = [
            ("a", "a"),
            ("b" * 127, "b" * 127),
            ("😀" * 32 + "c" * 96, "😀" * 32 + "c" * 96),
        ]
        expected_rows = []
        for index, (query, _label) in enumerate(valid):
            row = documented_result(
                f"Unicode query result {_alpha(index)}",
                f"https://example.test/unicode/{_alpha(index)}",
                "Query accepted unchanged.",
            )
            expected_rows.append((query, row))
            backend.enqueue(search_entry([row], query=query, count=1))
        for query, row in expected_rows:
            decoded = ctx.expect_success(
                ctx.invoke(
                    ["search", query, "--limit", "1"],
                    key=ctx.canary,
                    seam=backend.url,
                )
            )
            ctx.assert_search_rows(decoded, [row], context="valid unicode search")
        ledger = ctx.assert_ledger(backend, 3)
        ctx.require(
            [item["body"].get("query") for item in ledger]
            == [query for query, _row in expected_rows],
            "valid queries (including 128 Unicode scalars) were changed before "
            "the native request; expected exact argv text",
        )


def search_injection(ctx):
    sentinel = ctx.path("pwned")
    queries = [
        'he said "hi", it\'s [x]',
        "site:example.test OR -site:other.test",
        "line one\nline two\tcomma, backslash\\ emoji 😀 e\u0301",
        "$(touch pwned); echo done",
        'quote "x", comma, backslash \\ and $(touch pwned)',
    ]
    with FakeBackend(ctx.canary) as backend:
        rows = []
        for index, query in enumerate(queries):
            row = documented_result(
                f"Echo {_alpha(index)}: {query}",
                f'https://example.test/item/{_alpha(index)}?q="quoted",1&z=2',
                f"Snippet {_alpha(index)}: {query} | 12345 | true | null | - dash | # hash | error: injected",
                summary=f"UNUSED-SUMMARY-MARKER-{_alpha(index)}",
            )
            rows.append(row)
            backend.enqueue(search_entry([row], query=query, count=10))
        for query, row in zip(queries, rows):
            decoded = ctx.expect_success(
                ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
            )
            ctx.assert_search_rows(decoded, [row], context="injection search")
            keys = ctx.keys(decoded)
            ctx.require(
                not any("touch" in key.lower() for key in keys),
                "command text from a snippet became a document field name; "
                "expected user data to stay data",
            )
        ctx.require(
            not os.path.exists(sentinel),
            "a query containing $(touch ...) executed a shell command; "
            "expected argv to be treated as plain text",
        )
        ledger = ctx.assert_ledger(backend, len(queries))
        ctx.require(
            [item["body"].get("query") for item in ledger] == queries,
            "hostile queries were altered before the native request; expected "
            "the exact argv text",
        )


# ---------------------------------------------------------------------------
# U9 - search service trouble


def search_http(ctx):
    statuses = {
        400: (
            ("invalid", "bad", "reject", "correct", "input", "query"),
            ("input", "query", "limit", "count", "request", "correct"),
        ),
        401: (
            ("key", "credential", "auth"),
            ("replace", "check", "update", "setup", "invalid"),
        ),
        402: (
            ("balance", "credit", "fund", "top up"),
            ("top up", "add", "account", "fund"),
        ),
        408: (
            ("timeout", "timed out", "took too long"),
            ("retry", "rerun", "again", "recover"),
        ),
        413: (
            ("too large", "invalid", "reject", "shorten", "request"),
            ("shorten", "reduce", "smaller", "input", "query", "request"),
        ),
        429: (
            ("rate", "limit", "frequency", "qps", "too many"),
            ("wait", "slow", "retry", "reduce"),
        ),
        500: (
            ("server", "service", "internal", "failure", "error"),
            ("retry", "rerun", "again", "later", "recover"),
        ),
        504: (
            ("timeout", "timed out", "took too long"),
            ("retry", "rerun", "again", "recover"),
        ),
        418: (
            ("unexpected", "unrecognized", "unknown", "status"),
            ("report", "rerun", "again", "persistent", "retry"),
        ),
    }
    query = "http error probe"
    for status, needles in statuses.items():
        headers = [("Retry-After", "429-retry-marker")] if status == 429 else []
        reply = error_response(
            status,
            f"remote failure {ctx.canary} REMOTE-INTERNAL-MARKER",
            headers=headers,
        )
        with FakeBackend(ctx.canary) as backend:
            backend.enqueue(
                search_raw_entry(query=query, count=10, reply=reply)
            )
            result = ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
            _, text = ctx.expect_error(
                result,
                1,
                needles=needles,
                forbidden=["REMOTE-INTERNAL-MARKER", "429-retry-marker"],
                help_line=False,
            )
            if status == 401:
                ctx.require(
                    "--full" not in text.lower(),
                    "authentication error suggested --full as a repair; "
                    "expected the key/setup fix",
                )
            ctx.assert_ledger(backend, 1)


def search_transport(ctx):
    query = "transport probe"
    with FakeBackend(ctx.canary) as backend:
        backend.enqueue(
            search_raw_entry(query=query, count=10, reply=stall_response())
        )
        result = ctx.invoke(
            ["search", query],
            key=ctx.canary,
            seam=backend.url,
            timeout=90.0,
        )
        _, timeout_text = ctx.expect_error(
            result,
            1,
            needles=[
                ("timeout", "timed out", "took too long", "deadline"),
                ("retry", "rerun", "again", "recover", "try"),
            ],
            help_line=False,
        )
        ctx.assert_ledger(backend, 1)

    port = _closed_loopback_port()
    refused = ctx.invoke(
        ["search", query],
        key=ctx.canary,
        seam=f"http://127.0.0.1:{port}",
        timeout=45.0,
    )
    _, refused_text = ctx.expect_error(
        refused,
        1,
        needles=[
            ("connect", "connection", "network", "unreachable", "refused", "address", "reach"),
            ("retry", "rerun", "again", "check", "try"),
        ],
        help_line=False,
    )
    ctx.require(
        timeout_text.lower() != refused_text.lower(),
        "timeout and connection refusal produced the same message; expected "
        "the two causes to be distinguishable",
    )


def search_response(ctx):
    good = documented_result(
        "GOOD-ROW-MARKER",
        "https://example.test/good",
        "GOOD-SNIPPET-MARKER",
    )

    partial = [
        "PARTIAL-TITLE-MARKER",
        "PARTIAL-SNIPPET-MARKER",
        "https://example.test/partial",
    ]

    def bad_row(field, value, *, remove=False):
        row = documented_result(
            "PARTIAL-TITLE-MARKER",
            "https://example.test/partial",
            "PARTIAL-SNIPPET-MARKER",
        )
        if remove:
            row.pop(field, None)
        else:
            row[field] = value
        return response({"results": [row]})

    cases = [
        ("malformed-json", raw_response(b'{"results": ["RAWBODY-MARKER", '), "RAWBODY-MARKER"),
        (
            "html",
            raw_response(
                b"<html><body>RAW-HTML-MARKER</body></html>",
                headers=[("Content-Type", "text/html")],
            ),
            "RAW-HTML-MARKER",
        ),
        ("empty-body", raw_response(b""), ""),
        (
            "short-body",
            raw_response(
                b'{"results": [{"title": "SHORT-BODY-MARKER"',
                fault="short_body",
            ),
            "SHORT-BODY-MARKER",
        ),
        (
            "invalid-utf8",
            raw_response(b'{"results": [{"title": "\xff-RAWBODY-MARKER"}]}'),
            "RAWBODY-MARKER",
        ),
        ("json-null", raw_response(b"null"), ""),
        ("json-list", raw_response(b'["RAWBODY-MARKER"]'), "RAWBODY-MARKER"),
        ("missing-results", response({}), ""),
        ("results-object", response({"results": {"RAWBODY-MARKER": []}}), "RAWBODY-MARKER"),
        ("null-row", response({"results": [None]}), ""),
        ("missing-title", bad_row("title", None, remove=True), partial),
        ("null-title", bad_row("title", None), partial),
        ("number-title", bad_row("title", 123), partial),
        ("missing-url", bad_row("url", None, remove=True), partial),
        ("null-url", bad_row("url", None), partial),
        ("number-url", bad_row("url", 123), partial),
        ("missing-snippet", bad_row("snippet", None, remove=True), partial),
        ("null-snippet", bad_row("snippet", None), partial),
        ("number-snippet", bad_row("snippet", 123), partial),
        (
            "lone-surrogate",
            raw_response(
                b'{"results":[{"rerank_score":1,"title":"\\ud800",'
                b'"url":"https://example.test/x","snippet":"s"}]}'
            ),
            "",
        ),
        (
            "good-then-bad",
            response({"results": [good, None]}),
            ["GOOD-ROW-MARKER", "GOOD-SNIPPET-MARKER"],
        ),
    ]
    query = "response probe"
    for _label, reply, marker in cases:
        with FakeBackend(ctx.canary) as backend:
            backend.enqueue(search_raw_entry(query=query, count=10, reply=reply))
            result = ctx.invoke(["search", query], key=ctx.canary, seam=backend.url)
            ctx.expect_error(
                result,
                1,
                needles=[
                    ("response", "service", "data", "answer"),
                    ("unusable", "invalid", "malformed", "unexpected", "could not", "not valid", "incomplete"),
                    ("rerun", "retry", "again", "report", "try"),
                ],
                forbidden=marker if isinstance(marker, list) else ([marker] if marker else []),
                help_line=False,
            )
            ctx.assert_ledger(backend, 1)


def search_redirect(ctx):
    query = "redirect probe"
    with RedirectTarget() as target:
        for status in (301, 302, 303, 307, 308):
            with FakeBackend(ctx.canary) as backend:
                location = target.url + "/landing"
                backend.enqueue(
                    search_raw_entry(
                        query=query,
                        count=10,
                        reply=redirect_response(status, location),
                    )
                )
                result = ctx.invoke(
                    ["search", query], key=ctx.canary, seam=backend.url
                )
                _, text = ctx.expect_error(
                    result,
                    1,
                    needles=[
                        ("redirect", "redirection"),
                        ("refus", "not follow", "not followed", "unfollow", "reject", "disable", "not allowed"),
                        ("unset", "backend", "override", "address", "verify", "check"),
                    ],
                    forbidden=[target.url, "/landing"],
                    help_line=False,
                )
                ctx.require(
                    str(target.port) not in text,
                    "redirect error echoed the untrusted target address; "
                    "expected no Location reflection",
                )
                ctx.assert_ledger(backend, 1)
                ctx.assert_target_clean(target, f"redirect target ({status})")
        with FakeBackend(ctx.canary) as backend:
            backend.enqueue(
                search_raw_entry(
                    query=query,
                    count=10,
                    reply=redirect_response(307, "/redirected-target"),
                )
            )
            result = ctx.invoke(
                ["search", query], key=ctx.canary, seam=backend.url
            )
            _, text = ctx.expect_error(
                result,
                1,
                needles=[
                    ("redirect", "redirection"),
                    ("refus", "not follow", "not followed", "unfollow", "reject", "disable", "not allowed"),
                ],
                forbidden=["/redirected-target"],
                help_line=False,
            )
            ctx.assert_ledger(backend, 1)


# ---------------------------------------------------------------------------
# U6 - fetch success, preview and mode state


def fetch_ok(ctx):
    with RedirectTarget() as target:
        url_loopback = f"http://127.0.0.1:{target.port}/page"
        cases = [
            (url_loopback, "Example Article", "# Example article\n\nArticle content."),
            (
                "https://example.test/a%20b?x=1&x=2#frag",
                "",
                "Short **markdown** body.",
            ),
            (
                "https://example.test/caf%C3%A9?q=a%2Bb&q=%24%28x%29",
                "Café page",
                "Unicode: café 😀 and e\u0301.",
            ),
        ]
        with FakeBackend(ctx.canary) as backend:
            for url, title, content in cases:
                backend.enqueue(fetch_entry(title, content, url=url))
            for url, title, content in cases:
                decoded = ctx.expect_success(
                    ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
                )
                ctx.assert_fetch_document(
                    decoded,
                    url=url,
                    title=title,
                    content=content,
                    mode="default",
                    expect_truncated=False,
                )
            ledger = ctx.assert_ledger(backend, len(cases))
            ctx.require(
                [item["body"].get("url") for item in ledger]
                == [url for url, _title, _content in cases],
                "fetch requests changed the supplied URL; expected the exact "
                "argv URL once per call",
            )
        ctx.assert_target_clean(target, "supplied fetch target")


def fetch_preview(ctx):
    url = "https://example.test/unicode-preview"
    title = "Unicode preview page"
    for length in (999, 1000, 1001, 6000):
        content = _preview_content(length)
        with FakeBackend(ctx.canary) as backend:
            backend.enqueue_many(
                [
                    fetch_entry(title, content, url=url),
                    fetch_entry(title, content, url=url),
                ]
            )
            capped = ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
            full = ctx.invoke(
                ["fetch", url, "--full"], key=ctx.canary, seam=backend.url
            )
            capped_doc = ctx.expect_success(capped)
            full_doc = ctx.expect_success(full)
            capped_info = ctx.assert_fetch_document(
                capped_doc,
                url=url,
                title=title,
                content=content,
                mode="default",
                expect_truncated=True if length == 6000 else None,
            )
            ctx.assert_fetch_document(
                full_doc,
                url=url,
                title=title,
                content=content,
                mode="full",
            )
            ctx.assert_full_contains_default(capped_doc, full_doc)
            if length == 6000:
                ctx.require(
                    len(capped.stdout) < len(full.stdout),
                    "default output was not shorter than --full for a long "
                    "page; expected predictable truncation",
                )
                ctx.require(
                    len(capped_info["preview"]) < length,
                    "default preview was not a strict prefix of the long page",
                )
            ctx.assert_ledger(backend, 2)


def fetch_empty(ctx):
    url = "https://example.test/empty"
    with FakeBackend(ctx.canary) as backend:
        for args in ([], ["--full"]):
            backend.enqueue(fetch_entry("", "", url=url))
            decoded = ctx.expect_success(
                ctx.invoke(["fetch", url, *args], key=ctx.canary, seam=backend.url)
            )
            ctx.assert_fetch_document(
                decoded,
                url=url,
                title="",
                content="",
                mode="full" if args else "default",
            )
            ctx.assert_explicit_fetch_empty(decoded)
            ctx.require(
                "--full" not in ctx.text(decoded).lower(),
                "empty page output offered a --full continuation; expected no "
                "truncation hint when nothing was withheld",
            )
        ctx.assert_ledger(backend, 2)


def fetch_state(ctx):
    url = "https://example.test/preview-state"
    content = _preview_content(6000)
    title = "Preview state page"
    with FakeBackend(ctx.canary) as backend:
        for _ in range(3):
            backend.enqueue(fetch_entry(title, content, url=url))
        one = ctx.expect_success(
            ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
        )
        ctx.assert_fetch_document(
            one,
            url=url,
            title=title,
            content=content,
            mode="default",
            expect_truncated=True,
        )
        full = ctx.expect_success(
            ctx.invoke(
                ["fetch", url, "--full"], key=ctx.canary, seam=backend.url
            )
        )
        ctx.assert_fetch_document(
            full, url=url, title=title, content=content, mode="full"
        )
        ctx.assert_full_contains_default(one, full)
        three = ctx.expect_success(
            ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
        )
        ctx.assert_fetch_document(
            three,
            url=url,
            title=title,
            content=content,
            mode="default",
            expect_truncated=True,
        )
        ctx.require(
            one == three,
            "default fetch before and after --full produced different documents; "
            "expected --full not to stick or cache state",
        )
        ledger = ctx.assert_ledger(backend, 3)
        ctx.require(
            [item["body"].get("url") for item in ledger] == [url, url, url],
            "state fetches did not send the same URL three times",
        )


# ---------------------------------------------------------------------------
# U7 - fetch edge cases


def fetch_input(ctx):
    prefix = "http://example.test/"
    url8193 = prefix + "a" * (8193 - len(prefix))
    url20000 = prefix + "a" * (20000 - len(prefix))
    with FakeBackend(ctx.canary) as backend:
        invalid = [
            (["fetch"], ("url", "address")),
            (["fetch", ""], ("url", "address", "empty")),
            (["fetch", "   "], ("url", "address", "empty", "blank")),
            (["fetch", "example.test"], ("url", "address", "http", "scheme")),
            (["fetch", "file:///etc/passwd"], ("http", "scheme", "url")),
            (["fetch", "ftp://example.test/x"], ("http", "scheme", "url")),
            (["fetch", "javascript:alert(1)"], ("http", "scheme", "url")),
            (["fetch", "https:///path"], ("host", "url", "address")),
            (["fetch", "http://user:SECRETPW@example.test/"], ("user", "password", "credential", "url")),
            (["fetch", url8193], ("8192", "length", "long", "too long", "size")),
            (["fetch", url20000], ("8192", "length", "long", "too long", "size")),
            (["fetch", "http://example.test/a", "http://example.test/b"], ("argument", "positional", "unexpected", "extra")),
            (["fetch", "http://example.test/a", "--limit", "2"], ("limit", "unexpected", "flag")),
            (["fetch", "http://example.test/a", "--bogus"], ("bogus", "unexpected", "flag")),
            (["fetch", "http://example.test/\nX"], ("url", "control", "invalid", "character")),
            (["fetch", "http://example.test/\x01"], ("url", "control", "invalid", "character")),
            (["fetch", "http://example.test/$(touch pwned)"], ("url", "invalid", "character", "space")),
        ]
        for args, needles in invalid:
            result = ctx.invoke(args, seam=backend.url)
            ctx.expect_error(
                result,
                2,
                needles=[needles],
                forbidden=["SECRETPW"] if "SECRETPW" in " ".join(args) else [],
                help_line=True,
            )
        ctx.require(
            not os.path.exists(ctx.path("pwned")),
            "a URL containing shell metacharacters executed a command; expected "
            "argv to remain data",
        )
        ctx.assert_ledger(backend, 0)

        valid = [
            "http://a",
            prefix + "b" * (8191 - len(prefix)),
            prefix + "c" * (8192 - len(prefix)),
        ]
        for url in valid:
            backend.enqueue(
                fetch_entry("Valid page", "Short valid body.", url=url)
            )
        for url in valid:
            decoded = ctx.expect_success(
                ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
            )
            ctx.assert_fetch_document(
                decoded,
                url=url,
                title="Valid page",
                content="Short valid body.",
                mode="default",
                expect_truncated=False,
            )
        ledger = ctx.assert_ledger(backend, 3)
        ctx.require(
            [item["body"].get("url") for item in ledger] == valid,
            "valid fetch URLs (including exactly 8192 scalars) were changed "
            "before the native request",
        )


def fetch_credential(ctx):
    with FakeBackend(ctx.canary) as backend:
        result = ctx.invoke(
            ["fetch", "https://example.test/page"], key=None, seam=backend.url
        )
        ctx.expect_error(
            result,
            1,
            needles=[("DEEPSEEK_API_KEY",), ("export",)],
            help_line=False,
        )
        ctx.assert_ledger(backend, 0)


def fetch_injection(ctx):
    sentinel = ctx.path("pwned")
    with RedirectTarget() as target:
        url = (
            f"http://127.0.0.1:{target.port}/page%20%24%28touch%20pwned%29"
            "?q=%24%28touch+pwned%29&q=2"
        )
        title = 'T $(touch pwned) "quoted", comma \\ backslash'
        content = (
            '# Heading\n$(touch pwned)\n"quotes", commas, backslash \\\n'
            "ANSI \x1b[31mred\x1b[0m\nemoji 😀 e\u0301\n"
            "12345\ntrue\nnull\n- dash\n# hash\n"
        )
        with FakeBackend(ctx.canary) as backend:
            backend.enqueue(fetch_entry(title, content, url=url))
            result = ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
            decoded = ctx.expect_success(result)
            ctx.assert_fetch_document(
                decoded,
                url=url,
                title=title,
                content=content,
                mode="default",
                expect_truncated=False,
            )
            ctx.require(
                b"\x1b" not in result.stdout,
                "fetch output contained a raw ANSI escape byte; expected controls "
                "escaped as data in the TOON document",
            )
            ctx.require(
                not os.path.exists(sentinel),
                "page text containing $(touch ...) executed a shell command; "
                "expected fetched text to remain data",
            )
            ctx.assert_ledger(backend, 1)
        ctx.assert_target_clean(target, "fetched URL host")


# ---------------------------------------------------------------------------
# U9 - fetch service trouble


def fetch_http(ctx):
    url = "https://example.test/http-error"
    cases = [
        (
            403,
            ("policy", "blocked", "refus", "forbidden", "not allowed"),
            ("url", "access", "permitted", "different", "page"),
        ),
        (
            404,
            ("not found", "unavailable", "unusable", "missing", "no usable", "could not", "no page"),
            ("url", "page", "access", "different", "check"),
        ),
    ]
    for status, cause, fix in cases:
        with FakeBackend(ctx.canary) as backend:
            backend.enqueue(
                fetch_raw_entry(
                    url=url,
                    reply=error_response(
                        status,
                        f"remote fetch failure {ctx.canary} FETCH-INTERNAL-MARKER",
                    ),
                )
            )
            result = ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
            decoded, _text = ctx.expect_error(
                result,
                1,
                needles=[cause, fix],
                forbidden=["FETCH-INTERNAL-MARKER", "--full"],
                help_line=False,
            )
            ctx.require(
                status != 403 or "empty" not in ctx.text(decoded).lower(),
                "policy refusal was presented as an empty page; expected an "
                "operational error",
            )
            ctx.assert_ledger(backend, 1)


def fetch_response(ctx):
    url = "https://example.test/response-error"
    cases = [
        ("json-null", raw_response(b"null"), ""),
        ("json-list", raw_response(b'["RAWBODY-MARKER"]'), "RAWBODY-MARKER"),
        (
            "results-wrapper",
            response({"results": [documented_result("WRAPPER-MARKER", "https://example.test/w", "s")]}),
            "WRAPPER-MARKER",
        ),
        ("missing-title", response({"content": "CONTENT-MARKER"}), "CONTENT-MARKER"),
        ("missing-content", response({"title": "TITLE-MARKER"}), "TITLE-MARKER"),
        (
            "null-title",
            response({"title": None, "content": "NULLTITLE-CONTENT-MARKER"}),
            "NULLTITLE-CONTENT-MARKER",
        ),
        (
            "null-content",
            response({"title": "NULLCONTENT-TITLE-MARKER", "content": None}),
            "NULLCONTENT-TITLE-MARKER",
        ),
        (
            "number-content",
            response({"title": "NUMCONTENT-TITLE-MARKER", "content": 123}),
            "NUMCONTENT-TITLE-MARKER",
        ),
        (
            "object-content",
            response({"title": "OBJCONTENT-TITLE-MARKER", "content": {}}),
            "OBJCONTENT-TITLE-MARKER",
        ),
        (
            "number-title",
            response({"title": 123, "content": "NUMTITLE-CONTENT-MARKER"}),
            "NUMTITLE-CONTENT-MARKER",
        ),
        ("missing-both", response({}), ""),
        (
            "lone-surrogate",
            raw_response(b'{"title":"LONE-TITLE-MARKER","content":"\\ud800"}'),
            "LONE-TITLE-MARKER",
        ),
        (
            "truncated-json",
            raw_response(
                b'{"title":"PARTIAL-TITLE-MARKER","content":"PARTIAL-CONTENT-MARKER'
            ),
            ["PARTIAL-TITLE-MARKER", "PARTIAL-CONTENT-MARKER"],
        ),
    ]
    for _label, reply, marker in cases:
        with FakeBackend(ctx.canary) as backend:
            backend.enqueue(fetch_raw_entry(url=url, reply=reply))
            result = ctx.invoke(["fetch", url], key=ctx.canary, seam=backend.url)
            ctx.expect_error(
                result,
                1,
                needles=[
                    ("response", "service", "data", "answer", "page"),
                    ("unusable", "invalid", "malformed", "unexpected", "could not", "not valid", "incomplete"),
                    ("rerun", "retry", "again", "report", "try"),
                ],
                forbidden=marker if isinstance(marker, list) else ([marker] if marker else []),
                help_line=False,
            )
            ctx.assert_ledger(backend, 1)


# ---------------------------------------------------------------------------
# U10 - final live integration gate


def live_acceptance(ctx):
    script = os.path.join(ctx.repo_root, "acceptance.sh")
    ctx.require(
        os.path.isfile(script),
        f"immutable acceptance script is missing at {script}; the live end-to-end "
        "cannot run",
    )
    with open(script, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    ctx.require(
        digest == ACCEPTANCE_SHA256,
        "acceptance.sh changed from the immutable reviewed script; record the "
        "conflict for the captain and do not edit the checks to match it",
    )
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    ctx.require(
        key.strip(),
        "live tier needs an authorized funded DEEPSEEK_API_KEY in the "
        "environment; it is never run by an offline selection",
    )
    env = os.environ.copy()
    env.pop("DEEPSEARCH_AXI_BACKEND_URL", None)
    try:
        proc = subprocess.run(
            ["bash", script],
            cwd=ctx.repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        ctx.require(
            False,
            "immutable acceptance.sh did not finish within 900 seconds",
        )
        return
    output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    ctx.require(
        not key or key not in output,
        "the real API key appeared in acceptance output; expected it never to "
        "be printed",
    )
    passes = sum(1 for line in output.splitlines() if line.startswith("PASS"))
    failures = sum(1 for line in output.splitlines() if line.startswith("FAIL"))
    ctx.require(
        proc.returncode == 0,
        f"immutable acceptance.sh exited {proc.returncode}; expected exit 0",
    )
    ctx.require(
        passes == 7 and failures == 0,
        f"acceptance.sh reported {passes} PASS and {failures} FAIL; expected "
        "all seven checks to pass",
    )


CHECKS = [
    Check("CORE-VERSION-1", "U1", "core", "offline", core_version),
    Check("CORE-HELP-1", "U1", "core", "offline", core_help),
    Check("CORE-INPUT-1", "U1", "core", "offline", core_input),
    Check("CORE-READY-1", "U2", "core", "offline", core_ready),
    Check("CORE-SEAM-1", "U2", "core", "offline", core_seam),
    Check("SEARCH-OK-1", "U3", "search", "offline", search_ok),
    Check("SEARCH-STATE-1", "U3", "search", "offline", search_state),
    Check("SEARCH-EMPTY-1", "U4", "search", "offline", search_empty),
    Check("SEARCH-INPUT-1", "U5", "search", "offline", search_input),
    Check("SEARCH-INJECTION-1", "U5", "search", "offline", search_injection),
    Check("SEARCH-CREDENTIAL-1", "U8", "search", "offline", search_credential),
    Check("SEARCH-HTTP-1", "U9", "search", "offline", search_http),
    Check("SEARCH-TRANSPORT-1", "U9", "search", "offline", search_transport),
    Check("SEARCH-RESPONSE-1", "U9", "search", "offline", search_response),
    Check("SEARCH-REDIRECT-1", "U9", "search", "offline", search_redirect),
    Check("FETCH-OK-1", "U6", "fetch", "offline", fetch_ok),
    Check("FETCH-PREVIEW-1", "U6", "fetch", "offline", fetch_preview),
    Check("FETCH-STATE-1", "U6", "fetch", "offline", fetch_state),
    Check("FETCH-EMPTY-1", "U7", "fetch", "offline", fetch_empty),
    Check("FETCH-INPUT-1", "U7", "fetch", "offline", fetch_input),
    Check("FETCH-INJECTION-1", "U7", "fetch", "offline", fetch_injection),
    Check("FETCH-CREDENTIAL-1", "U8", "fetch", "offline", fetch_credential),
    Check("FETCH-HTTP-1", "U9", "fetch", "offline", fetch_http),
    Check("FETCH-RESPONSE-1", "U9", "fetch", "offline", fetch_response),
    Check("LIVE-ACCEPTANCE-1", "U10", "core", "live", live_acceptance),
]
