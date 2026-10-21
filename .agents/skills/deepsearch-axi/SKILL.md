---
name: deepsearch-axi
description: "Search the web and fetch pages as Markdown through DeepSeek with compact TOON output. Use when a task needs current web results, source URLs, or the contents of a specific page."
---

# deepsearch-axi

Agent-native CLI for DeepSeek web search and page fetch with compact TOON output.

## When to use

- A task needs current web results, source URLs, or the contents of a specific page.
- Local readiness is useful before research: run `deepsearch-axi` with no arguments.
- Skip it when the repository already answers the question or no web evidence is needed.

## Setup-free invocation

Run from a Git checkout without installing:

```sh
uvx --from git+https://github.com/ddbaron/deepsearch-axi.git deepsearch-axi search "<query>"
```

Set `DEEPSEEK_API_KEY` in the environment, in `./.env`, or in `~/.config/deepsearch-axi/.env` before searching or fetching.

## Commands

### deepsearch-axi search

Search the web through DeepSeek and print compact TOON results.

```text
deepsearch-axi search <query> [flags]
```

- `<query>`: 1-128 characters, passed unchanged including operators

| Flag | Default | Description |
| --- | --- | --- |
| `--count N` | `10` | number of results to return (1-50) |
| `--need-content` | `false` | request page content for each result |
| `--ori-question TEXT` | `omitted` | original question used for reranking (1-128 characters) |
| `--localization IP` | `(empty string)` | IPv4 or IPv6 localization hint |
| `--fields FIELD,...` | `title,url,snippet` | comma-separated ordered result fields |
| `--full` | `false` | remove text preview limits for the selected fields |
| `--help` | `false` | show help and exit |

Examples:

- `deepsearch-axi search "rust async runtime after:2025-01-01"`
- `deepsearch-axi search '"structured concurrency" -site:reddit.com' --count 5 --fields title,url,summary,date`
- `deepsearch-axi search -- "-site:example.com tokio"`

### deepsearch-axi fetch

Fetch a page as Markdown through DeepSeek and print compact TOON.

```text
deepsearch-axi fetch <url> [flags]
```

- `<url>`: http(s) URL up to 8192 characters

| Flag | Default | Description |
| --- | --- | --- |
| `--full` | `false` | remove text preview limits for the page fields |
| `--help` | `false` | show help and exit |

Examples:

- `deepsearch-axi fetch https://example.com/article`
- `deepsearch-axi fetch https://example.com/article --full`

### deepsearch-axi setup

Install or repair Claude Code, Codex, and OpenCode session integrations.

```text
deepsearch-axi setup [flags]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--agent NAME` | `all` | integration to configure (one host or all) |
| `--help` | `false` | show help and exit |

Examples:

- `deepsearch-axi setup`
- `deepsearch-axi setup --agent codex`

### deepsearch-axi session

Record a native session lifecycle event from host hook JSON on stdin.

```text
deepsearch-axi session <start|end> --agent NAME --project ABS_DIR
```

Subcommands: `start`, `end`.

| Flag | Default | Description |
| --- | --- | --- |
| `--help` | `false` | show help and exit |

Examples:

- `deepsearch-axi session start --agent codex --project "$PWD" < event.json`
- `deepsearch-axi session end --agent codex --project "$PWD" < event.json`

### deepsearch-axi session start

Record a session start and print compact ambient context from native hook JSON on stdin (max 64 KiB).

```text
deepsearch-axi session start --agent NAME --project ABS_DIR
```

| Flag | Default | Description |
| --- | --- | --- |
| `--agent NAME` | `required` | host integration reporting the event |
| `--project ABS_DIR` | `required` | absolute project directory |
| `--help` | `false` | show help and exit |

Examples:

- `deepsearch-axi session start --agent claude-code --project "$PWD" < event.json`
- `deepsearch-axi session start --agent opencode --project "$PWD" < event.json`

### deepsearch-axi session end

Record a session checkpoint or end from native hook JSON on stdin (max 64 KiB).

```text
deepsearch-axi session end --agent NAME --project ABS_DIR
```

| Flag | Default | Description |
| --- | --- | --- |
| `--agent NAME` | `required` | host integration reporting the event |
| `--project ABS_DIR` | `required` | absolute project directory |
| `--help` | `false` | show help and exit |

Examples:

- `deepsearch-axi session end --agent claude-code --project "$PWD" < event.json`
- `deepsearch-axi session end --agent opencode --project "$PWD" < event.json`

## Output, truncation, and empty results

Every document is TOON with 2-space indentation and LF line endings. Search rows contain only the selected fields, in order. Defaults are `title,url,snippet`; `--need-content` adds `content`; `--fields` replaces the projection with an ordered allowlist.

Text previews truncate to 200 code points for title and snippet and 1000 for summary and content. A `truncated` array reports `path`, `shown`, and `total` only when a value was cut. `--full` removes those preview limits but not the 8 MiB response cap, count limit, or schema validation.

An empty successful search reports `count: 0`, `total: 0`, `total_scope: response`, and `message: "No results returned for this query."` with a query-refinement hint; it is not an error.

## Errors and exit codes

Errors are TOON documents with an `error.code`, a safe `message`, and one or two actionable hints. Exit codes: `0` success, `1` execution or configuration failure (including all HTTP statuses), `2` local usage failure. No command prompts, and no raw response body, header, URL credential, or secret is printed.

## Project instruction

Use deepsearch-axi for DeepSeek web search and page fetch; run deepsearch-axi for local readiness and follow its contextual TOON hints.
