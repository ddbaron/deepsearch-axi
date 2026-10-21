"""Command dispatch, the single structured-error boundary and research counters.

``main`` parses arguments before any credential, file or network access, then
routes one command. Every expected failure becomes a safe error document; an
unexpected exception becomes the fixed ``internal_error`` document so no
traceback, dependency text or credential can reach stdout or stderr. Only
``render`` writes stdout.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

from deepsearch_axi import documents, render, responses, session, state, transport
from deepsearch_axi import home as local_home
from deepsearch_axi.argv import parse_args
from deepsearch_axi.auth import resolve_credential
from deepsearch_axi.failures import internal_error, local_io, status_failure
from deepsearch_axi.models import (
    EXIT_FAILURE,
    EXIT_OK,
    DeepsearchError,
    FetchCommand,
    HelpCommand,
    HomeCommand,
    JsonObject,
    LocalContext,
    SearchCommand,
    SessionCommand,
    SetupCommand,
    Tool,
    exit_code_for,
)
from deepsearch_axi.origin import validate_base_url


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> int:
    """Run one command and return its process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ if env is None else env)
    home_path = Path.home() if home is None else Path(home)

    document: JsonObject | None = None
    secrets: tuple[str, ...] = ()
    code = EXIT_OK
    cwd_path: Path | None = None
    attempts: list[Tool] = []
    attempt_secrets: list[str] = []
    succeeded = False

    try:
        cwd_path = _resolve_cwd(cwd)
        command = parse_args(arguments)
        if isinstance(command, HomeCommand):
            # The home view echoes cwd/bin/origin values that may reflect the
            # effective key (for example inside a directory or hostname). Hand
            # the resolved value only to the output boundary and to the state
            # scope lookup, which stores it only as an opaque identifier.
            secrets = _redaction_secrets(environment, cwd_path, home_path)
            context = local_home.gather_local_context(
                environment, cwd_path, home_path, secrets=secrets
            )
            document = documents.home_document(context)
        elif isinstance(command, HelpCommand):
            document = documents.help_document(command.path)
        elif isinstance(command, SearchCommand):
            document, secrets = _search(
                command, environment, cwd_path, home_path, attempts, attempt_secrets
            )
            succeeded = True
        elif isinstance(command, FetchCommand):
            document, secrets = _fetch(
                command, environment, cwd_path, home_path, attempts, attempt_secrets
            )
            succeeded = True
        elif isinstance(command, SetupCommand):
            document = _setup(command, environment, cwd_path, home_path)
        elif isinstance(command, SessionCommand):
            payload = session.read_native_payload(_stdin(stdin))
            session_secrets: list[str] = []
            secret_cache: dict[Path, tuple[str, ...]] = {}

            def secrets_for(path: Path) -> tuple[str, ...]:
                cached = secret_cache.get(path)
                if cached is None:
                    cached = _redaction_secrets(environment, path, home_path)
                    secret_cache[path] = cached
                    session_secrets.extend(cached)
                return cached

            def context_for(path: Path) -> LocalContext:
                return local_home.gather_local_context(
                    environment, path, home_path, secrets=secrets_for(path)
                )

            result = session.handle_session(
                command.event,
                command.agent,
                command.project,
                payload,
                context_for=context_for,
                secrets_for=secrets_for,
            )
            document = result.document
            secrets = tuple(dict.fromkeys(session_secrets))
        else:  # pragma: no cover - parse_args only returns the frozen union
            raise DeepsearchError(internal_error())
    except DeepsearchError as error:
        document = documents.error_document(error.failure)
        code = exit_code_for(error.failure)
    except Exception:
        document = documents.error_document(internal_error())
        code = EXIT_FAILURE
    finally:
        # Search/fetch failures still count once a request was attempted.
        if attempts and cwd_path is not None:
            _record_research(
                cwd_path,
                home_path,
                attempts[0],
                succeeded,
                tuple(attempt_secrets),
            )

    if document is not None:
        _write(document, secrets)
    return code


def _stdin(stream: BinaryIO | None) -> BinaryIO | None:
    if stream is not None:
        return stream
    return getattr(sys.stdin, "buffer", None)


def _resolve_cwd(cwd: Path | None) -> Path:
    try:
        return Path.cwd() if cwd is None else Path(cwd)
    except (OSError, RuntimeError, ValueError):
        raise DeepsearchError(local_io(".")) from None


def _redaction_secrets(env: Mapping[str, str], cwd: Path, home: Path) -> tuple[str, ...]:
    """Return the effective credential for redaction only.

    Home and session views expose paths and the API origin, which may reflect
    the key even though ``LocalContext`` never stores it. Resolution mirrors
    the search/fetch precedence (env, exact-cwd dotenv, user dotenv); missing
    or invalid configuration contributes no secret and stays a probe concern.
    """
    try:
        credential = resolve_credential(env, cwd, home)
    except DeepsearchError:
        return ()
    return (credential.value,)


def _search(
    command: SearchCommand,
    env: Mapping[str, str],
    cwd: Path,
    home: Path,
    attempts: list[Tool],
    used_secrets: list[str],
) -> tuple[JsonObject, tuple[str, ...]]:
    origin = validate_base_url(env.get("DEEPSEEK_BASE_URL"))
    credential = resolve_credential(env, cwd, home)
    secrets = (credential.value,)
    used_secrets.append(credential.value)
    # From here a request is attempted, so the finally block must count either
    # outcome even when post_tool or response validation raises.
    attempts.append("search")
    reply = transport.post_tool(
        origin,
        "search",
        transport.build_search_body(command.options),
        credential,
    )
    if reply.status != 200:
        raise DeepsearchError(status_failure(reply.status, "search"))
    rows = responses.validate_search_response(reply.payload or {}, command.options.count)
    document = documents.search_document(
        command.options.query,
        rows,
        command.output,
        request=command.options,
        secrets=secrets,
    )
    return document, secrets


def _fetch(
    command: FetchCommand,
    env: Mapping[str, str],
    cwd: Path,
    home: Path,
    attempts: list[Tool],
    used_secrets: list[str],
) -> tuple[JsonObject, tuple[str, ...]]:
    origin = validate_base_url(env.get("DEEPSEEK_BASE_URL"))
    credential = resolve_credential(env, cwd, home)
    secrets = (credential.value,)
    used_secrets.append(credential.value)
    # See _search: failures past this point are attempted research calls.
    attempts.append("fetch")
    reply = transport.post_tool(
        origin,
        "fetch",
        transport.build_fetch_body(command.url),
        credential,
    )
    if reply.status != 200:
        raise DeepsearchError(status_failure(reply.status, "fetch"))
    page = responses.validate_fetch_response(reply.payload or {})
    document = documents.fetch_document(command.url, page, command.full, secrets=secrets)
    return document, secrets


def _setup(
    command: SetupCommand,
    env: Mapping[str, str],
    cwd: Path,
    home: Path,
) -> JsonObject | None:
    # Imported lazily so search, fetch, home and version never depend on the
    # setup engine or pay for its imports.
    from deepsearch_axi.executable import resolve_running_executable, select_command
    from deepsearch_axi.installer import setup_project

    executable = select_command(resolve_running_executable(), env)
    result = setup_project(cwd.resolve(), command.agent, executable)
    if not result.changed:
        return None
    return documents.setup_document(result, documents.collapse_home(str(result.root), str(home)))


def _record_research(cwd: Path, home: Path, tool: Tool, ok: bool, secrets: tuple[str, ...]) -> None:
    try:
        state_root = state.find_state_root(cwd, home, secrets=secrets)
        if state_root is not None:
            state.record_research(state_root.root, state_root.scope, tool, ok)
    except (DeepsearchError, OSError):
        # Counters are best-effort and must never change the command result.
        return


def _write(document: JsonObject, secrets: tuple[str, ...]) -> None:
    try:
        rendered = render.render_document(document, secrets)
    except Exception:
        fallback = documents.error_document(internal_error())
        rendered = render.render_document(fallback, ())
    if rendered:
        render.write_output(rendered)


__all__ = ["main"]
