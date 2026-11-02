# Human-owned acceptance checks

This directory contains the executable end-to-end acceptance checks for the
deepsearch-axi trial build: the approved user stories U1-U10 and the eight
invariants P1-P8. It is the behavioral definition of done. The captain owns
it. Coding agents never edit it, never weaken it to make a build pass, and
never write their own substitute tests.

Expected results come from the approved stories and the documented API shapes,
never from running the product. The checks are independent of the build: they
invoke it as an installed command and observe only its process channels, the
files it writes, and the backend wire.

A clash between the checks and the product is recorded for the captain and
left in place. It is never "fixed" by editing the checks to match the product.

## What it contains

- `run.py` - a standard-library runner (plus `toon-format`) that invokes the
  installed command only as a subprocess. It never imports product code.
- `fake_backend.py` - a loopback-only fake for the two documented native
  endpoints, `POST /v1/tools/search` and `POST /v1/tools/fetch`. It holds a
  scripted queue of exact responses or faults, records every request (method,
  path, parsed body, canary match; never the raw key), and provides the
  second-listener proof that counts arrivals of any method after a redirect.
- `checks.py` - one end-to-end check for each of the 25 approved draft
  scenarios, tagged with its story, scenario ID, capability and tier.

The runner is black-box. It does not mock the product's HTTP library, TOON
encoder or internal functions.

## Invariants

P1, P2 and P5 are applied around every offline invocation; P3 and P4 are
asserted by every successful Fetch and Search. P6-P8 are metamorphic: they
compare two outputs produced by the same fake backend and assert no remembered
expected output.

- **P1 - the key never appears.** stdout, stderr, written files and the
  retained request ledger are scanned for the canary key and its fragments.
- **P2 - output is a valid agent-readable document.** Every normal result is
  decoded independently with the maintained `toon-format` package in strict
  mode.
- **P3 - the preview is honest.** The default Fetch view is a prefix of the
  full text, the shown length is the true length, and the `--full` notice
  appears exactly when text was withheld.
- **P4 - counts and limits hold.** Search never returns more than the limit
  asked for, the shown count equals what is listed, and no total is invented.
- **P5 - only the requested work happens.** Valid commands make exactly one
  request, local commands make none, redirects are not followed, there are no
  retries, and nothing is written to disk.
- **P6 - full contains default.** For the same fake page, the `--full` view
  contains the default view as a prefix. Checked by `FETCH-PREVIEW-1` and
  `FETCH-STATE-1` (story U6).
- **P7 - limit N is a prefix of limit N+1.** From one scripted answer,
  `search --limit N` returns exactly the first N rows returned by
  `search --limit N+1`. Checked by `SEARCH-OK-1` (story U3).
- **P8 - unused fields do not leak into results.** Two scripted answers with
  the same title, link and snippet but different unused backend fields produce
  identical search output. Checked by `SEARCH-OK-1` (story U3).

`--list` prints each check's story and scenario ID and the invariant mapping,
including the P6-P8 story tags.

## Install the runner interpreter

The runner needs the maintained `toon-format` package for its own independent,
strict TOON decoding. The product's encoder and the checks' decoder are not
the same code path.

Use any Python 3.11+ interpreter that can install it:

```sh
python3 -m venv .checks-env
.checks-env/bin/pip install toon-format
.checks-env/bin/python acceptance-checks/run.py --exe .venv/bin/deepsearch-axi
```

Alternatively, install it for the interpreter you already use:

```sh
python3 -m pip install toon-format
python3 acceptance-checks/run.py --exe .venv/bin/deepsearch-axi
```

The default executable is `.venv/bin/deepsearch-axi`. Pass `--exe` to point at
any other installed build. The product must already be installed; the checks
do not build it.

## Run subsets

From the repository root:

```sh
CHECKS=.checks-env/bin/python

# Everything offline (the normal coding-loop gate)
$CHECKS acceptance-checks/run.py

# One capability at a time
$CHECKS acceptance-checks/run.py --capability core
$CHECKS acceptance-checks/run.py --capability search
$CHECKS acceptance-checks/run.py --capability fetch

# Any combination, or a story
$CHECKS acceptance-checks/run.py --capability core --capability fetch
$CHECKS acceptance-checks/run.py --story U6
$CHECKS acceptance-checks/run.py --capability fetch --story U6

# Mapping of checks to stories and P1-P8 (runs nothing)
$CHECKS acceptance-checks/run.py --list

# The final live end-to-end gate, only after explicit authorization
$CHECKS acceptance-checks/run.py --tier live
```

Offline is the default tier; live work is never selected implicitly. A run
prints one line per check with its story and scenario ID and exits 0 only when
every selected check passes.

The offline runner puts each invocation in a fresh temporary HOME, working
directory and XDG tree, closes stdin, uses a random canary key, and on macOS
runs under a `sandbox-exec` profile that allows loopback to the fake backend
and denies every non-local outbound connection. The real API is never used by
an offline check.

The live check runs the unchanged, hash-checked `acceptance.sh` with the test
seam explicitly unset and a real funded `DEEPSEEK_API_KEY`. It is the final
end-to-end integration gate only and is never run in the coding loop.

## The two fixed names

The checks invoke the product, so they fix exactly two names:

1. The readiness command is **`doctor`**.
2. The test-only backend address variable is **`DEEPSEARCH_AXI_BACKEND_URL`**.

Unset means the real address `https://api.deepseek.com`. Set means an absolute
origin (optional trailing slash) to which the fixed `/v1/tools/...` paths are
appended. Plain HTTP is accepted only for a literal loopback IP. Empty or
invalid values fail closed with a corrective error; they never fall back to
production. There is no backend flag and no second variable.

## Tolerated product choices

The goal left four choices to the builder, so the checks assert invariants, not
those choices:

- **Preview length** is not a fixed number. For a nonempty page of length `L`,
  the default view must be a prefix of the full text, the shown length must
  equal the true length, the `--full` notice must appear exactly when text was
  withheld, and a long page must be shorter in default mode. The live
  acceptance script remains the place that bounds its own fixture. The offline
  checks do not assert a universal 4,000-byte cap.
- **Network timeout** is not a fixed number. A stalled backend must produce a
  clear non-zero error without a trace within the runner's bounded wait; the
  runner does not require a particular second count.
- **Search result fields** are not asserted by name. Rows are found by their
  fixture titles, links and snippets in order. P8 requires different unused
  backend fields to leave the selected result identical.
- **Readiness name** is fixed only because the checks must invoke it: `doctor`.

Other deliberate tolerances:

- Empty Fetch pages need an explicit plain-words empty state; the exact count
  rendering and field spelling are not asserted.
- HTTP error fixtures use a generic documented error envelope. The checks do
  not invent or assert status-specific `type`/`code` values.
- Hint wording is checked by meaning (cause, action, broader query, higher
  limit), not by a fixed sentence.
- The status of an empty search is checked by the explicit no-match statement
  and truthful zero count, not by a required field name.

The live check records a clash instead of editing either side: if
`acceptance.sh` no longer matches its reviewed SHA-256, the check fails and
the captain must decide. The checks are never updated to match a changed
product or script without that decision.
