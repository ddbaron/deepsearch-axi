#!/usr/bin/env bash
# Acceptance checks for the trial build. Run from anywhere; the script works
# relative to its own location. Exits 0 only when every check passes.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
CLI="$VENV/bin/deepsearch-axi"
PASS=0
FAIL=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass() { printf 'PASS  %-28s %s\n' "$1" "${2:-}"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL  %-28s %s\n' "$1" "${2:-}"; FAIL=$((FAIL + 1)); }

if [ ! -x "$CLI" ]; then
  python3 -m venv "$VENV" >/dev/null 2>&1 || true
  "$VENV/bin/pip" install -q -e "$ROOT" >/dev/null 2>&1 || true
fi

if [ ! -x "$CLI" ]; then
  printf 'FAIL  %-28s %s\n' "install" "no .venv/bin/deepsearch-axi after pip install -e ."
  printf '\n%d passed, %d failed\n' "$PASS" "$((FAIL + 1))"
  exit 1
fi

# 1. --version exits 0 and prints something
out="$("$CLI" --version 2>&1)"; rc=$?
if [ $rc -eq 0 ] && [ -n "${out//[[:space:]]/}" ]; then
  pass "--version" "$(printf '%s' "$out" | head -1)"
else
  fail "--version" "exit $rc"
fi

# 2. search returns 1..N records for --limit N, one per line
out="$("$CLI" search "python packaging" --limit 3 2>&1)"; rc=$?
lines="$(printf '%s\n' "$out" | grep -c '[^[:space:]]' || true)"
urls="$(printf '%s\n' "$out" | grep -c 'http' || true)"
if [ $rc -eq 0 ] && [ "$urls" -ge 1 ] && [ "$urls" -le 3 ]; then
  pass "search --limit 3" "$lines records"
else
  fail "search --limit 3" "exit $rc, $lines non-empty lines, $urls with a url"
fi

# 3. an empty query is refused with a help line and no traceback
out="$("$CLI" search "" 2>&1)"; rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qi 'help[: ]' && ! printf '%s' "$out" | grep -q 'Traceback'; then
  pass "search empty query" "exit $rc with help"
else
  fail "search empty query" "exit $rc"
fi

# 4. fetch truncates and names the way to get the rest
"$CLI" fetch https://docs.python.org/3/library/argparse.html >"$TMP/fetch.out" 2>&1; rc=$?
capped=$(wc -c <"$TMP/fetch.out" | tr -d ' ')
if [ $rc -eq 0 ] && [ "$capped" -gt 0 ] && [ "$capped" -lt 4000 ] && grep -qi -- '--full' "$TMP/fetch.out"; then
  pass "fetch truncates" "$capped bytes, names --full"
else
  fail "fetch truncates" "exit $rc, $capped bytes"
fi

# 5. --full returns more than the capped call
"$CLI" fetch https://docs.python.org/3/library/argparse.html --full >"$TMP/full.out" 2>&1; rc=$?
full=$(wc -c <"$TMP/full.out" | tr -d ' ')
if [ $rc -eq 0 ] && [ "$full" -gt "$capped" ]; then
  pass "fetch --full" "$full bytes vs $capped"
else
  fail "fetch --full" "exit $rc, $full bytes"
fi

# 6. --help names both commands
out="$("$CLI" --help 2>&1)"; rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q 'search' && printf '%s' "$out" | grep -q 'fetch'; then
  pass "--help" "lists search and fetch"
else
  fail "--help" "exit $rc"
fi

# 7. a missing credential is refused by name, with no traceback and no secret
bindir="$TMP/nohome"
mkdir -p "$bindir"
out="$(env -u DEEPSEEK_API_KEY HOME="$bindir" "$CLI" search "test" 2>&1)"; rc=$?
if [ $rc -ne 0 ] && ! printf '%s' "$out" | grep -q 'Traceback' && printf '%s' "$out" | grep -qiE 'key|setup|credential'; then
  pass "missing credential" "exit $rc, names the setup step"
else
  fail "missing credential" "exit $rc"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
