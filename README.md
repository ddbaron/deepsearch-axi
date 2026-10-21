# deepsearch-axi

Agent-native CLI for DeepSeek web search and page fetch with compact TOON output.

`deepsearch-axi` is a small Python 3.10+ command-line tool for agents and operators. It sends exactly one authenticated JSON request to DeepSeek's raw search or fetch tool endpoint, then prints one compact TOON document designed for model context: minimal default schemas, explicit expansion, deterministic truncation metadata, truthful response-scoped totals, structured errors, and contextual next steps. It never prompts, never writes stdout from anything except the document renderer, and never follows redirects or retries a request.

## Install

The package is installed from this repository. It is not published on PyPI.

```sh
pipx install git+https://github.com/ddbaron/deepsearch-axi.git
uv tool install git+https://github.com/ddbaron/deepsearch-axi.git
python -m pip install git+https://github.com/ddbaron/deepsearch-axi.git
```

Python 3.10 or newer is required. The only runtime dependencies are `toon-format==1.2.0` and `tomlkit`, which is used only for comment-preserving Codex TOML merges. `python -m deepsearch_axi` is an equivalent entrypoint to the installed `deepsearch-axi` console script.

## Quick start

```sh
export DEEPSEEK_API_KEY=<your-deepseek-api-key>
deepsearch-axi
deepsearch-axi search "site:docs.python.org asyncio TaskGroup"
deepsearch-axi fetch https://docs.python.org/3/library/asyncio-task.html
```

Running `deepsearch-axi` with no arguments is a local readiness view. It reports the executable, description, actual cwd, local credential state and source label, API origin readiness, and integration readiness without making a network call or reading a response. It exits 0 even when the key is missing.

## Commands

### `deepsearch-axi`

With no arguments, prints the local readiness document described above. The sole-argument aliases `-v`, `-V`, and `--version` print exactly the package version plus one LF and exit 0 without loading the CLI, TOON, auth, or transport modules. Trailing version flags are usage errors.

### `deepsearch-axi search <query>`

Searches the web through DeepSeek and prints compact TOON results. The query is preserved code point for code point, including `site:`, `-site:`, negative terms, negative quoted phrases, quoted phrases, `OR`, `lang:`, `after:`, and `before:` operators, backslashes, repeated spaces, and Unicode. Use the standard `--` delimiter when a query begins with a hyphen.

Search examples:

```sh
deepsearch-axi search "site:docs.python.org asyncio TaskGroup"
deepsearch-axi search '"structured concurrency" -site:reddit.com' --count 5 --fields title,url,summary,date
deepsearch-axi search -- "-site:example.com tokio"
```

`--count` accepts 1-50 and defaults to 10. `--need-content` alone adds a fourth `content` column. An explicit `--fields` projection replaces the defaults and preserves the requested order, so a projection wins over `--need-content`. `--fields` draws from an ordered allowlist of `title`, `url`, `snippet`, `summary`, `content`, `date`, `authority`, `rerank_score`, `site_name`, and `site_icon`. Space around comma-separated names is trimmed, while the query itself is never trimmed. `--ori-question` supplies a 1-128 character reranking question. `--localization` accepts an IPv4 or IPv6 address and defaults to empty. `--full` removes text preview limits for the selected fields.

### `deepsearch-axi fetch <url>`

Fetches a page as Markdown through DeepSeek and prints compact TOON. The input must be an HTTP(S) URL with a host, without userinfo, without control characters, invalid ports, or more than 8192 characters. The URL is sent unchanged as a JSON argument to the authenticated API; the CLI never makes a direct request to the page. The displayed `page.url` is always the input URL because the API returns no redirect target.

Fetch examples:

```sh
deepsearch-axi fetch https://example.com/article
deepsearch-axi fetch https://example.com/article --full
```

### `deepsearch-axi setup [--agent NAME]`

Explicitly installs or repairs project-scoped session integrations for Claude Code, Codex, and OpenCode. `--agent` defaults to `all`; a singular selection modifies only that host and tool-owned project state. Setup is the only command that registers or repairs integrations. Normal home, search, fetch, help, and version commands never create or modify host settings, plugins, skills, or repository instruction files.

Setup merges native `SessionStart` and `SessionEnd` handlers into `.claude/settings.json` and `.codex/hooks.json`, ensures `[features].hooks = true` in `.codex/config.toml` while preserving unrelated TOML values and comments, and manages the dual-compatibility plugin at `.opencode/plugins/deepsearch-axi.js`. Tool-owned records live in `.deepsearch-axi/manifest.json`. Preflight conflicts, malformed host configuration, symlink escapes, and write failures leave destinations unchanged; a repeated identical setup preserves bytes and mtimes and emits zero bytes.

Examples:

```sh
deepsearch-axi setup
deepsearch-axi setup --agent codex
```

### `deepsearch-axi session start|end --agent NAME --project ABS_DIR`

`session` is the documented integration protocol for native host hooks, not a research command. It reads bounded native hook JSON from non-TTY stdin (maximum 64 KiB), records a hashed lifecycle event, and prints the compact ambient context for start. It never reads a transcript, never calls the network, and never stores raw payloads, prompts, queries, URLs, or page content. Other internal errors exit 1 so a host wrapper never treats local readiness as a blocking usage error.

Session examples:

```sh
deepsearch-axi session start --agent claude-code --project "$PWD" < event.json
deepsearch-axi session end --agent claude-code --project "$PWD" < event.json
```

### Help and version

`--help` is valid at every command and group level, returns TOON with the command's complete valid flags, defaults, required arguments, and examples, and neither reads credential files nor accesses the network. Per-command help wins over a missing required positional, but unknown tokens are still usage errors.

## Flag reference

Every parser flag and its default is listed here. `--help` is implicit on every command and defaults to `false`.

| Command | Flag | Default | Description |
| --- | --- | --- | --- |
| `root` | `-v, -V, --version` | `n/a` | print the installed version and exit (sole argument only) |
| `search` | `--count N` | `10` | number of results to return (1-50) |
| `search` | `--need-content` | `false` | request page content for each result |
| `search` | `--ori-question TEXT` | `omitted` | original question used for reranking (1-128 characters) |
| `search` | `--localization IP` | `(empty string)` | IPv4 or IPv6 localization hint |
| `search` | `--fields FIELD,...` | `title,url,snippet` | comma-separated ordered result fields |
| `search` | `--full` | `false` | remove text preview limits for the selected fields |
| `fetch` | `--full` | `false` | remove text preview limits for the page fields |
| `setup` | `--agent NAME` | `all` | integration to configure (one host or all) |
| `session start` | `--agent NAME` | `required` | host integration reporting the event |
| `session start` | `--project ABS_DIR` | `required` | absolute project directory |
| `session end` | `--agent NAME` | `required` | host integration reporting the event |
| `session end` | `--project ABS_DIR` | `required` | absolute project directory |

There is no `--json` mode, no `--api-key` flag, no abbreviated long option, no flag passthrough, and no positional joining.

## Configuration and security

### Key precedence

The API key is resolved in this exact order, and lower-precedence sources are not read after a usable higher-precedence source:

1. A nonblank `DEEPSEEK_API_KEY` process environment variable.
2. `./.env` in the exact current working directory.
3. `~/.config/deepsearch-axi/.env` using the process home.

Parent `.env` files are never searched. Empty or whitespace-only candidates mean absent; a missing file means absent. A nonempty key containing CR, LF, or other header controls is invalid configuration and does not fall through to a lower source. Unreadable, non-UTF-8, oversized, duplicate, or malformed relevant assignments are structured `invalid_config` errors rather than silent precedence changes. No command accepts a key flag, prompts, or echoes key material.

### Literal dotenv grammar

Dotenv files are read as UTF-8 with literal semantics. Blank lines and `#` comment lines are ignored. An optional `export` prefix is accepted. The first `=` separates the key from the value, surrounding assignment whitespace is ignored, and one matching pair of single or double quotes is removed. On unquoted values, a whitespace-preceded `#` starts a comment; quoted values preserve `#`. There are no shell escapes, command substitutions, interpolations, variable expansions, `source`, `eval`, subprocess execution, multiline assignments, or environment mutations. Unrelated keys are ignored, but duplicate `DEEPSEEK_API_KEY` assignments or unmatched quotes on that assignment are errors.

### API origin

`DEEPSEEK_BASE_URL` is a process-environment-only setting that changes the API origin, not a dotenv key and not an arbitrary path prefix. Unset or empty uses `https://api.deepseek.com`. An optional trailing slash is accepted; `/v1` prefixes, non-root paths, queries, fragments, userinfo, and malformed values are rejected before any credential-bearing request.

An HTTPS origin accepts the official hostname or any trusted custom hostname, with an optional port. It is an operator-selected trusted endpoint and credential recipient that receives the API key, so enable it only when you trust the endpoint and its TLS chain; default certificate verification remains enabled. HTTP is permitted only for numeric loopback development origins, for example `http://127.0.0.1:8080` or `http://[::1]:8080`; `localhost` and nonnumeric hostnames are rejected for HTTP before a credential-bearing request. Environment proxies, netrc credentials, and automatic redirect following of any status or origin are never used.

### Bounded requests and redaction

Each command performs exactly one POST with a fixed 30-second timeout and no retry loop. A 200 response is read to at most 8 MiB plus one byte; larger responses fail `response_too_large`. Non-200 response bodies are never read, parsed, logged, or printed; status alone maps to the documented error code. The effective key is recursively redacted from every visible string, including reflected content and transport diagnostics, before encoding and truncation. Stderr is sanitized through the same policy.

### Local state and privacy

Research counters and lifecycle records live only under an ignored project `.deepsearch-axi/` directory created after explicit setup. Session identifiers are hashed before use; state is bounded to 64 KiB per file and at most 20 recent session records. State contains no credential material, queries, URLs, response content, prompts, transcript contents, or full native payloads. Views are cwd-scoped and never disclose sibling project activity. State writes use a file lock and atomic replacement so concurrent invocations cannot leave partial JSON.

## Output contract

### Documents and schemas

Every normal, error, and help document is one UTF-8 TOON document with 2-space indentation, comma delimiters, and LF line endings. No banners, ANSI, progress, raw JSON, or raw Markdown are emitted, and the renderer appends no document newline (the bare-version fast path is the only newline exception).

A default search document has `query`, `count`, `total`, `total_scope`, `results`, and `help`. Each result row contains exactly the selected fields in the requested order. A fetch detail has `page: {url, title, content}` and no next-step hint when the detail is complete. An error has `error: {code, message, http_status?}` plus one or two actionable `help` items.

### Counts, projection, and content

`count = total = len(results)` and `total_scope` is always `response`. This API returns no backend-wide total, pagination cursor, or result IDs, so the CLI never invents one. Order, duplicates, and legitimate empty or nullable values are preserved.

Defaults are exactly three columns: `title,url,snippet`. `--need-content` alone adds a fourth `content` column. An explicit `--fields` projection replaces the defaults and preserves the requested order, so a projection wins over `--need-content`. Selecting `content` without `--need-content` leaves the request's `need_content` false and returns a hint explaining the explicit opt-in rather than silently changing the request.

### Truncation and `--full`

Default per-field Unicode code-point limits are title 200, snippet 200, summary 1000, and content 1000. Values are emitted as exact prefixes with no appended ellipsis. When any emitted value was cut, the document gains a `truncated` array whose entries report `path`, `shown`, and `total`, and an ordinary hint is replaced by the matching `--full` template. URLs, dates, numeric fields, and query values are never truncated.

`--full` removes the text preview limits for the selected fields only. It does not bypass the 8 MiB response cap, the 1-50 count limit, schema validation, or redaction, and it does not change the HTTP request body or add unselected fields.

### Character and token cost

Default 10-row text previews contribute at most 4000 code points, excluding URLs, the query of up to 128 characters, and TOON envelope escaping. A selected summary or content field adds at most 1000 code points per row. Fetch defaults to at most 1200 title and content code points plus the URL and envelope. For ordinary English prose, characters/4 is an illustrative token estimate, not a tokenizer guarantee; URLs, Unicode, and escaping vary.

### Empty results, errors, and exit codes

An empty successful search returns exit 0 with `count: 0`, `total: 0`, `results: []`, and `message: "No results returned for this query."`, plus a query-refinement hint. It is not an auth, API, or absence proof.

Errors are TOON documents with a stable `error.code`, a safe message, an optional `http_status`, and one or two actionable hints. Exit codes are 0 for success, 1 for execution or configuration failures (including every HTTP status), and 2 for local usage failures. No command prompts, and no stack trace, dependency error, raw response body, header, redirect location, URL credential, or secret reaches either output channel.

## Agent integration

### Hook setup (recommended)

Hooks are the primary integration because they deliver ambient context automatically. Run `deepsearch-axi setup` from the project root after installing the CLI. Use `deepsearch-axi setup --agent claude-code`, `--agent codex`, or `--agent opencode` to select one host. Setup resolves the running console executable, preferring a bare `deepsearch-axi` name only when PATH resolves to the same file, otherwise installing a correctly quoted absolute path. Re-running setup after relocation repairs owned start and end hooks, plugin commands, and the manifest together without touching unrelated user configuration.

Claude Code and Codex receive plain TOON stdout from native `SessionStart` and `SessionEnd` commands. OpenCode uses the managed plugin with documented V1 `server()` behavior beginning at version 1.18.29 and the V2 `setup(ctx)` context-hook API; older unsupported versions are outside the tested boundary. Start succeeds with exit 0 even when the key is missing so a host session is never blocked by local readiness.

### Optional skill installation

Hooks are recommended first; the static discovery skill is a complementary, lower-overhead option. Install it with the Skills CLI:

```sh
npx -y skills add ddbaron/deepsearch-axi --skill deepsearch-axi
```

The committed `.agents/skills/deepsearch-axi/SKILL.md` is generated deterministically from the same static guidance module used by home and help, so it carries no live readiness, cwd, executable, state, or key values. Its trigger-shaped frontmatter, non-interactive repository-sourced examples, and freshness are checked in CI. Verify or regenerate the committed file from a source checkout with:

```sh
python scripts/generate_skill.py --check
python scripts/generate_skill.py
```

`--check` is read-only: it exits 0 only when the committed bytes are fresh and exits 1 when the file is missing or stale. The generator is a development command, not a second installed executable. The README instruction below is for users' own projects; this repository does not install it globally or edit agent instruction files.

### One-line project instruction

Copy this into your own project's agent instruction file:

```text
Use deepsearch-axi for DeepSeek web search and page fetch; run deepsearch-axi for local readiness and follow its contextual TOON hints.
```

## Worked example

The following transcript is illustrative fixture output, not a live call. The URL is the kind of result an operator would select from a real response; the next command substitutes it with `<url>`.

```sh
deepsearch-axi search 'site:docs.python.org "TaskGroup" after:2024-01-01' --count 5 --fields title,url,summary,date,authority
```

```text
query: "site:docs.python.org \"TaskGroup\" after:2024-01-01"
count: 1
total: 1
total_scope: response
results[1]{title,url,summary,date,authority}:
  asyncio TaskGroup,"https://docs.python.org/3/library/asyncio-task.html",Task groups combine a task creation API with a convenient way to wait for all tasks.,null,4
help[2]: deepsearch-axi fetch <url>,"deepsearch-axi search \"<query> site:<domain>\" --count 5 --fields title,url,summary,date,authority"
```

The operator selects the returned URL and uses it in the next command. Run the plain fetch first; add `--full` only if the fetch output's `truncated` array reports that a field was cut. The CLI never makes a direct request to the page and never invents a redirect target.

```sh
deepsearch-axi fetch <url>
deepsearch-axi fetch <url> --full
```

## AXI principles

1. **Token-efficient output** — a pinned maintained TOON encoder at the only output boundary, with internal JSON-shaped models and structured errors; no banners, progress, or raw JSON.
2. **Minimal default schemas** — three default result columns, an explicit ordered projection, count 10, and content only by opt-in.
3. **Content truncation** — exact preview limits, machine-readable `truncated` metadata, and a full-output escape only when content was actually cut.
4. **Precomputed aggregates** — response `count`, `total`, and `total_scope` are available in one call without fabricated web-wide totals or extra requests.
5. **Definitive empty states** — an explicit contextual zero-result success with a refinement template, never silent output or an implied retry.
6. **Structured errors and exit codes** — TOON errors on stdout, actionable repair hints, 0/1/2 semantics, no prompts, and unknown-argument rejection before side effects.
7. **Ambient session context** — explicit three-host setup, real native context injection, portable and repaired registrations, consumed lifecycle capture, and a generated secondary skill.
8. **Content first** — a no-arguments live local readiness and prior-project view instead of a help dump or a billable probe.
9. **Contextual disclosure** — bounded, relevant command templates with placeholders and retained option context, and no needless complete-detail hints.
10. **Consistent help and identity** — executable and one-sentence identity first, per-command flags/defaults/examples, and three bare fast-version aliases.

## Development

From a source checkout:

```sh
python -m pip install -r requirements-dev.txt -e .
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
rumdl check --disable MD013 README.md .agents/skills/deepsearch-axi/SKILL.md
python scripts/generate_skill.py --check
python -m build
python -m twine check dist/*
```

The test suite is offline: it uses fake keys, isolated HOME/XDG roots, loopback stub servers, and a socket guard inherited by child Python processes. CI runs the same checks on CPython 3.10, 3.11, 3.12, 3.13, and 3.14 on Linux plus a macOS job, installs the built wheel in isolated environments, and exercises the Node-based native plugin adapters. No workflow requires a secret, live API call, publishing permission, or release step.

## License

MIT License. See `LICENSE` for the full text. Copyright (c) 2026 Derek Baron.
