# User stories and properties

The checks in this directory use the story numbers U1 to U10 and the property
numbers P1 to P8. This file records what each number means in plain words.
The stories and properties P1 to P5 come from the approved story list. P6 to
P8 describe behavior the checks verify and were added while those checks were
built.

## User stories

### U1: Discover the tool

**What the user does:** Runs `deepsearch-axi` with no arguments, with
`--help`, or with `--version`.

**What must be true:**

- `--version` prints only the version number.
- `--help` lists `search`, `fetch` and `doctor` (the readiness check), each
  with a short example, so the tool can be used without other documentation.
- Running the command with no arguments shows what the tool is and what to do
  next.
- An unknown command or option exits non-zero and names a command to run
  instead.
- None of these paths needs a key or makes a network call.

**Checks:** CORE-VERSION-1, CORE-HELP-1, CORE-INPUT-1.

### U2: Check readiness

**What the user does:** Runs `deepsearch-axi doctor` before using the tool.

**What must be true:**

- With a key set, `doctor` reports that the tool is ready and says that it
  checked locally only, without confirming that the key works with DeepSeek.
- With no key, an empty key or a blank key, it exits non-zero and names
  `DEEPSEEK_API_KEY` and the exact setup step.
- It never prints the key or any part of it.
- It makes no network call.

**Checks:** CORE-READY-1, CORE-SEAM-1.

### U3: Search the web

**What the user does:** Runs `deepsearch-axi search "python packaging"`,
optionally with a limit.

**What must be true:**

- The result is a short ranked list. Each item has a title, a link and a
  snippet.
- The count shown is the number of results returned. It is never more than the
  limit asked for, and no total is invented.
- If more results may exist, a short hint says how to ask for more.
- Asking twice gives the same kind of answer, and a failed search does not
  affect the next one.
- The query typed by the user reaches the service unchanged.

**Checks:** SEARCH-OK-1, SEARCH-STATE-1.

### U4: Search finds nothing

**What the user does:** Searches for something the service has no matches for.

**What must be true:**

- The output says explicitly that there were no results and suggests
  broadening the query. It is not a blank screen and not an error.
- No results are made up.

**Checks:** SEARCH-EMPTY-1.

### U5: Search with bad input

**What the user does:** Gives an empty query, a limit that is zero, negative
or too large, or text full of quotes and shell characters.

**What must be true:**

- Bad input exits non-zero, says what was wrong, gives a help line that says
  how to fix it, and sends no request.
- Odd characters in a valid query are treated as plain text and sent
  unchanged.
- A bad query never causes a request, a crash or a stack trace.

**Checks:** SEARCH-INPUT-1, SEARCH-INJECTION-1.

### U6: Fetch a page

**What the user does:** Runs `deepsearch-axi fetch <url>`, or the same command
with `--full`.

**What must be true:**

- By default the output gives the page title, a short preview of the text, a
  plain truncation notice, the total length and a hint that names `--full`.
- With `--full`, the output gives the whole text and no truncation notice.
- The preview is always a clean prefix of the full text.
- A short page is shown whole with no truncation notice.
- A default fetch followed by a `--full` fetch of the same page gives
  consistent results.
- Nothing is added to or removed from the page text beyond the preview cut.

**Checks:** FETCH-OK-1, FETCH-PREVIEW-1, FETCH-STATE-1.

### U7: Fetch edge cases

**What the user does:** Fetches an empty page, or gives a bad address.

**What must be true:**

- An empty page is reported as empty, in plain words, in both default and
  `--full` modes.
- A bad or unsupported address, an address with a login in it, or text full of
  shell characters exits non-zero with a help line and sends no request.
- A fetch never returns a blank result that looks like a success without
  saying so.

**Checks:** FETCH-EMPTY-1, FETCH-INPUT-1, FETCH-INJECTION-1.

### U8: Missing credential on search or fetch

**What the user does:** Runs `search` or `fetch` with no key set.

**What must be true:**

- The command exits non-zero and names the setup step, the same one the
  readiness check gives.
- An empty or blank key is treated as missing.
- No request is sent, and the key is never printed.

**Checks:** SEARCH-CREDENTIAL-1, FETCH-CREDENTIAL-1.

### U9: The service has trouble

**What the user does:** Meets one of these conditions: DeepSeek rejects the
key, the account is out of balance, a page is blocked or missing, the request
is rate limited, the service errors, is slow or unreachable, answers with
garbage or the wrong shape, or redirects.

**What must be true:**

- Each case exits non-zero with an error that says what happened in plain
  terms and names the fix, such as checking the key, topping up the balance,
  trying a different page, or waiting and retrying.
- A garbled or unusable answer is reported as unusable and is not shown half
  parsed.
- A redirect is not followed. Exactly one request reaches the service, the
  redirect target gets nothing, and the key is not forwarded.
- There is no stack trace, no raw server text, no automatic retry, and the key
  never appears in an error.

**Checks:** SEARCH-HTTP-1, SEARCH-TRANSPORT-1, SEARCH-RESPONSE-1,
SEARCH-REDIRECT-1, FETCH-HTTP-1, FETCH-RESPONSE-1.

### U10: Final live check

**What the user does:** Installs the finished tool and runs the acceptance
script live against DeepSeek.

**What must be true:**

- All seven checks in the acceptance script pass.
- The script is unchanged.
- This is the final integration check only. It is never run in the coding
  loop.

**Checks:** LIVE-ACCEPTANCE-1.

## Properties

### P1: The key never appears

The key never appears in any output, error, help text or file the tool writes,
whatever the input or service behavior.

Checked around every offline invocation.

### P2: Output is always a valid agent-readable document

Every normal result is one well-formed TOON document on stdout. Quotes, commas
and newlines in titles or text never corrupt it.

Checked on every offline result.

### P3: The preview is honest

For any page, the default view is a prefix of the full text. The length shown
is the true length. The truncation notice appears exactly when text was
withheld.

Checked by every successful fetch.

### P4: Counts and limits hold

Search never returns more than the limit asked for, and the count shown equals
what is listed.

Checked by every successful search.

### P5: Only the requests asked for, nothing else

Valid network commands make exactly one request. Help, version and readiness
make none. Nothing is retried, followed or written to disk.

Checked around every offline invocation.

### P6: Full contains default

For the same page, the `--full` view contains the default view as a prefix.
Checked by FETCH-PREVIEW-1 and FETCH-STATE-1 in story U6.

Added while the checks were built. It is not in the approved property list.

### P7: Limit N is a prefix of limit N+1

From one scripted answer, `search --limit N` returns exactly the first N rows
returned by `search --limit N+1`. Checked by SEARCH-OK-1 in story U3.

Added while the checks were built. It is not in the approved property list.

### P8: Unused fields do not leak into results

Two scripted answers with the same title, link and snippet but different
unused backend fields produce identical search output. Checked by SEARCH-OK-1
in story U3.

Added while the checks were built. It is not in the approved property list.

## Choices that were settled

The stories left four choices open. The shipped product fixes them as follows:

- The readiness check is the `doctor` command. The checks invoke it by name.
- The network timeout is 30 seconds, with no retry. The offline checks require
  a clear error from a stalled backend but do not fix the number of seconds.
- The default fetch preview is the first 1,000 characters. The offline checks
  require the preview to be a true prefix but do not fix its length.
- Search results use the title, url (the link) and snippet fields by default.
  The checks locate rows by those values rather than by field name.
