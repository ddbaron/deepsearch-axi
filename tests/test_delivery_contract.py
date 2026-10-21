"""AC-43/AC-45 delivery-contract tests for the evidence verifier.

The tests run ``scripts/verify_evidence.py`` as a subprocess against copies of
the synthetic fixtures under ``tests/fixtures/evidence``. Mutations happen only
in ``tmp_path``; the committed fixtures are never modified.

Real evidence is read only through the explicit ``DEEPSEARCH_AXI_EVIDENCE_DIR``
environment variable. When it is unset, the bundled synthetic ``complete``
fixture is audited, so CI never depends on an operator directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evidence"
COMPLETE = FIXTURES / "complete"
FAILING = FIXTURES / "failing"
VERIFIER = REPO_ROOT / "scripts" / "verify_evidence.py"
EVIDENCE_ENV = "DEEPSEARCH_AXI_EVIDENCE_DIR"
HANDOFF_PHRASE = "no live api calls performed; managing crewmate acceptance pending"
REMOTE_SCOPE = "cannot prove absence of remote actions"
FEATURE_BRANCH = "feature/b11c205b-deepsearch-axi-cli"
PROTECTED_REFS = {"main"}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_BRANCHES = frozenset({"main", "master", "trunk", "fm/deepsearch-axi-build-r2"})
PYTHON_PROGRAM = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
GIT_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--exec-path",
        "--super-prefix",
        "--receive-pack",
        "--upload-pack",
        "--bundle-uri",
    }
)
GIT_GLOBAL_EQUALS_PREFIXES = (
    "--git-dir=",
    "--work-tree=",
    "--namespace=",
    "--config-env=",
    "--exec-path=",
    "--super-prefix=",
    "--receive-pack=",
    "--upload-pack=",
    "--bundle-uri=",
)
GIT_CURRENT_BRANCH_MUTATORS = frozenset(
    {"commit", "merge", "rebase", "cherry-pick", "revert", "reset", "am", "apply", "pull"}
)
GIT_BRANCH_MUTATION_FLAGS = frozenset(
    {"-f", "--force", "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy"}
)
GIT_BRANCH_READONLY_FLAGS = frozenset(
    {
        "-l",
        "--list",
        "-a",
        "--all",
        "-r",
        "--remotes",
        "--show-current",
        "-v",
        "--verbose",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--format",
        "--sort",
    }
)
GIT_CHECKOUT_CREATE_OPTIONS = frozenset({"-b", "-B", "-c", "-C", "--orphan"})
GIT_CHECKOUT_VALUE_OPTIONS = frozenset(
    {"--conflict", "--pathspec-from-file", "--pathspec-file-nul"}
)
GIT_UPDATE_REF_VALUE_OPTIONS = frozenset({"-m", "--message"})
GIT_REBASE_VALUE_OPTIONS = frozenset(
    {
        "--onto",
        "--exec",
        "-x",
        "--strategy",
        "-s",
        "--strategy-option",
        "-X",
        "--empty",
        "--whitespace",
    }
)
GIT_REBASE_VALUE_SHORTS = frozenset({"x", "s", "X"})
GIT_REBASE_FLAG_SHORTS = frozenset({"i", "v", "q", "m", "n", "p", "f", "r", "S", "k", "h"})
GIT_REBASE_FLAGS = frozenset(
    {
        "--interactive",
        "--verbose",
        "--quiet",
        "--stat",
        "--no-stat",
        "--verify",
        "--no-verify",
        "--force-rebase",
        "--no-ff",
        "--ff",
        "--root",
        "--fork-point",
        "--no-fork-point",
        "--keep-base",
        "--autosquash",
        "--no-autosquash",
        "--autostash",
        "--no-autostash",
        "--keep-empty",
        "--no-keep-empty",
        "--reapply-cherry-picks",
        "--no-reapply-cherry-picks",
        "--committer-date-is-author-date",
        "--ignore-date",
        "--reset-author-date",
        "--signoff",
        "--no-signoff",
        "--update-refs",
        "--no-update-refs",
        "--rerere-autoupdate",
        "--no-rerere-autoupdate",
        "--apply",
        "--merge",
        "--rebase-merges",
        "--no-rebase-merges",
        "--gpg-sign",
        "--help",
        "--version",
    }
)
GIT_REBASE_MODES = frozenset(
    {"--continue", "--abort", "--skip", "--edit-todo", "--quit", "--show-current-patch"}
)
GIT_FETCH_VALUE_OPTIONS = frozenset(
    {
        "--depth",
        "--shallow-since",
        "--shallow-exclude",
        "--deepen",
        "--negotiation-tip",
        "--upload-pack",
        "--jobs",
        "-j",
        "--server-option",
        "-o",
        "--filter",
        "--recurse-submodules-default",
    }
)
GIT_FETCH_FLAGS = frozenset(
    {
        "--all",
        "-a",
        "--append",
        "--atomic",
        "--dry-run",
        "--force",
        "-f",
        "--keep",
        "-k",
        "--multiple",
        "-m",
        "--no-tags",
        "-n",
        "--tags",
        "-t",
        "--prune",
        "-p",
        "--prune-tags",
        "-P",
        "--quiet",
        "-q",
        "--verbose",
        "-v",
        "--progress",
        "--no-progress",
        "--ipv4",
        "-4",
        "--ipv6",
        "-6",
        "--unshallow",
        "--update-shallow",
        "--refetch",
        "--prefetch",
        "--porcelain",
        "--set-upstream",
        "--show-forced-updates",
        "--no-show-forced-updates",
        "--write-fetch-head",
        "--no-write-fetch-head",
        "--update-head-ok",
        "-u",
        "--auto-maintenance",
        "--no-auto-maintenance",
        "--auto-gc",
        "--no-auto-gc",
        "--negotiate-only",
        "--recurse-submodules",
        "--no-recurse-submodules",
        "--help",
        "--version",
    }
)
GIT_PULL_EXTRA_FLAGS = frozenset(
    {
        "--rebase",
        "--no-rebase",
        "--ff",
        "--no-ff",
        "--ff-only",
        "--squash",
        "--commit",
        "--no-commit",
        "--edit",
        "-e",
        "--no-edit",
        "--allow-unrelated-histories",
        "--log",
        "--no-log",
        "--autostash",
        "--no-autostash",
        "--signoff",
        "--no-signoff",
        "--verify",
        "--no-verify",
        "--verify-signatures",
        "--no-verify-signatures",
        "--stat",
        "--no-stat",
        "--summary",
        "--no-summary",
        "--compact-summary",
        "--no-compact-summary",
        "--rebase-merges",
        "--no-rebase-merges",
        "--autosquash",
        "--no-autosquash",
    }
)
GIT_PULL_EXTRA_VALUE_OPTIONS = frozenset(
    {"--strategy", "-s", "--strategy-option", "-X", "--gpg-sign", "-S"}
)
GIT_FETCH_UNSAFE_OPTIONS = frozenset({"--mirror", "--refmap"})
SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
SHELL_CONTROL_KEYWORDS = frozenset(
    {
        "!",
        "{",
        "}",
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "function",
    }
)
SHELL_DEPTH_LIMIT = 3
REMOTE_WRITE_ACTIONS = frozenset(
    {"add", "remove", "rm", "rename", "set-head", "set-branches", "set-url", "prune", "update"}
)
GH_GLOBAL_VALUE_OPTIONS = frozenset({"-R", "--repo", "-H", "--hostname", "--config"})
GH_PR_WRITE_ACTIONS = frozenset(
    {"create", "merge", "close", "reopen", "edit", "ready", "review", "comment", "lock", "unlock"}
)
GH_RELEASE_WRITE_ACTIONS = frozenset({"create", "upload", "edit", "delete", "publish"})
GH_REPO_WRITE_ACTIONS = frozenset(
    {"create", "delete", "edit", "fork", "rename", "transfer", "archive", "unarchive", "sync"}
)
CONTAINER_READONLY_ACTIONS = frozenset(
    {
        "version",
        "info",
        "ps",
        "images",
        "inspect",
        "history",
        "logs",
        "top",
        "stats",
        "diff",
        "events",
        "search",
        "port",
    }
)
WRAPPER_PROGRAMS = frozenset(
    {"sudo", "env", "command", "builtin", "nohup", "time", "exec", "timeout"}
)
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
INSTRUCTION_FILE_WRITE = re.compile(
    r"(?:>>?|\btee\b(?:\s+-a)?|\b(?:touch|truncate|rm|unlink)\b"
    r"|\b(?:sed|perl)\b[^|;&]*?(?:-i|--in-place)"
    r"|\b(?:cp|mv|install|ln)\b|\bgit\s+(?:add|commit|rm|mv)\b)"
    r"[^|;&]*\b(?:AGENTS|CLAUDE)\.md\b",
    re.IGNORECASE,
)


def _load_verifier_module():
    """Load the verifier for direct auditor comparisons (never executed as a gate)."""
    spec = importlib.util.spec_from_file_location("verify_evidence_under_test", VERIFIER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/verify_evidence.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERIFIER_MODULE = _load_verifier_module()


def _verifier_issues(command: str, branch: str | None) -> list[str]:
    return VERIFIER_MODULE._audit_action_command(command, branch, set(PROTECTED_REFS))


def _run_verifier(
    artifacts: Path | None,
    *,
    env: dict[str, str],
    prd: Path | None = None,
    confirm: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(VERIFIER)]
    if artifacts is not None:
        command.extend(["--artifacts", str(artifacts)])
    if prd is not None:
        command.extend(["--prd", str(prd)])
    if confirm:
        command.append("--confirm")
    return subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _copy_complete(tmp_path: Path, name: str = "evidence") -> Path:
    destination = tmp_path / name
    shutil.copytree(COMPLETE, destination)
    # ac43_result.json (and its temporary file) are produced by a run and never
    # committed; drop any stale copy before exercising the verifier.
    for stale in ("ac43_result.json", ".ac43_result.json.tmp"):
        (destination / stale).unlink(missing_ok=True)
    return destination


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest(root: Path) -> dict:
    return _read_json(root / "manifest.json")


def _refs(root: Path) -> dict:
    return _read_json(root / "refs.json")


def _append_action(root: Path, command: str, *, branch: str | None = None) -> None:
    entries = [
        json.loads(line)
        for line in (root / "action_log.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    entry = {
        "seq": max(entry["seq"] for entry in entries) + 1,
        "command": command,
        "actor": "test-mutation",
    }
    if branch is not None:
        entry["branch"] = branch
    entries.append(entry)
    (root / "action_log.jsonl").write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in entries),
        encoding="utf-8",
    )


def _evidence_dir() -> Path:
    """Resolve real evidence only from the explicit environment variable."""
    configured = os.environ.get(EVIDENCE_ENV)
    return Path(configured).expanduser() if configured else COMPLETE


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^[-*]\s*{re.escape(name)}\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _section(text: str, name: str) -> str | None:
    pattern = rf"^#+\s+{re.escape(name)}\s*$(.*?)(?=^#+\s|\Z)"
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return match.group(1) if match else None


def _first_hex(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.search(r"[0-9a-f]{7,40}", value, re.IGNORECASE)
    return match.group(0).lower() if match else None


def _program_name(token: str) -> str:
    name = Path(token).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _shell_segments(command: str) -> list[list[str]]:
    """Independently split a recorded command into auditable segments.

    This deliberately does not import the verifier's splitter. Unsupported
    shell syntax raises ``ValueError`` so the caller fails closed.
    """
    segments: list[list[str]] = []
    current: list[str] = []
    word: list[str] = []
    word_started = False
    quote = ""
    position = 0

    def end_word() -> None:
        nonlocal word, word_started
        if word_started:
            current.append("".join(word))
        word = []
        word_started = False

    def end_segment() -> None:
        nonlocal current
        end_word()
        segments.append(current)
        current = []

    while position < len(command):
        char = command[position]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word.append(char)
            position += 1
            continue
        if quote == '"':
            if char == '"':
                quote = ""
            elif char == "\\" and position + 1 < len(command):
                following = command[position + 1]
                if following != "\n":
                    word.append(following if following in {"\\", '"', "$", "`"} else char)
                position += 2
                continue
            elif char == "`":
                raise ValueError("backtick command substitution is unsupported")
            elif (
                char == "$" and position + 1 < len(command) and command[position + 1] in {"(", "["}
            ):
                raise ValueError("dynamic substitution is unsupported")
            else:
                word.append(char)
            position += 1
            continue
        if char == "\\":
            if position + 1 >= len(command):
                raise ValueError("trailing escape is unsupported")
            if command[position + 1] != "\n":
                word_started = True
                word.append(command[position + 1])
            position += 2
            continue
        if char in {"'", '"'}:
            quote = char
            word_started = True
            position += 1
            continue
        if char == "`" or (
            char == "$" and position + 1 < len(command) and command[position + 1] in {"(", "["}
        ):
            raise ValueError("dynamic substitution is unsupported")
        if char == "&":
            if position + 1 < len(command) and command[position + 1] == "&":
                end_segment()
                position += 2
                continue
            raise ValueError("background '&' is unsupported")
        if char == "|":
            end_segment()
            position += 2 if position + 1 < len(command) and command[position + 1] == "|" else 1
            continue
        if char in {";", "\n"}:
            end_segment()
            position += 1
            continue
        if char in {"<", ">"}:
            raise ValueError("redirect or heredoc syntax is unsupported")
        if char in {"(", ")"}:
            raise ValueError("subshell or grouping syntax is unsupported")
        if char.isspace():
            end_word()
            position += 1
            continue
        word_started = True
        word.append(char)
        position += 1

    if quote:
        raise ValueError("unterminated quote is unsupported")
    end_segment()
    if any(not segment for segment in segments):
        raise ValueError("empty shell segment is unsupported")
    return segments


def _strip_prefixes(tokens: list[str]) -> tuple[list[str], bool]:
    elevated = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = _program_name(token)
        if ENV_ASSIGNMENT.match(token):
            index += 1
            continue
        if name == "sudo":
            elevated = True
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                option = tokens[index]
                index += 1
                if option in {"-u", "--user", "-g", "--group"} and index < len(tokens):
                    index += 1
            continue
        if name == "timeout":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            if index < len(tokens):
                index += 1
            continue
        if name in WRAPPER_PROGRAMS:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                option = tokens[index]
                index += 1
                if option in {"-u", "--unset", "-C", "--chdir"} and index < len(tokens):
                    index += 1
            continue
        break
    return tokens[index:], elevated


def _positionals(tokens: list[str], value_options: frozenset[str] = frozenset()) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            result.extend(tokens[index + 1 :])
            break
        if token in value_options:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        result.append(token)
        index += 1
    return result


def _first_positional(tokens: list[str], value_options: frozenset[str] = frozenset()) -> str | None:
    positionals = _positionals(tokens, value_options)
    return positionals[0] if positionals else None


def _split_git_global(tokens: list[str]) -> tuple[str | None, list[str]]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in GIT_GLOBAL_VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith(GIT_GLOBAL_EQUALS_PREFIXES):
            index += 1
            continue
        if token.startswith(("-C", "-c")) and len(token) > 2:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[index + 1 :]
    return None, []


def _normalize_ref(name: str | None) -> str:
    if not name:
        return ""
    value = name.strip().strip("'\"").strip()
    if value.startswith("+"):
        value = value[1:].strip()
    if value.endswith("^{}"):
        value = value[:-3]
    lowered = value.lower()
    for prefix in ("refs/heads/", "refs/remotes/", "heads/", "remotes/"):
        if lowered.startswith(prefix):
            value = value[len(prefix) :]
            lowered = value.lower()
            break
    if lowered.startswith("origin/"):
        value = value[len("origin/") :]
    return value.strip().lower()


def _protected_names(protected: set[str]) -> set[str]:
    return {_normalize_ref(name) for name in protected} | {
        _normalize_ref(name) for name in DEFAULT_BRANCHES
    }


def _is_protected(name: str | None, protected: set[str]) -> bool:
    return bool(name) and _normalize_ref(name) in protected


def _rebase_targets(rest: list[str]) -> tuple[set[str], list[str]]:
    """Return explicitly named rebase branch operands, failing closed if unclear."""
    positionals: list[str] = []
    modes: set[str] = set()
    saw_root = False
    position = 0
    while position < len(rest):
        token = rest[position]
        if token == "--":
            positionals.extend(rest[position + 1 :])
            break
        if token.startswith("--"):
            name, separated, _ = token.partition("=")
            if name in GIT_REBASE_MODES:
                modes.add(name)
                position += 1
                continue
            if name in GIT_REBASE_VALUE_OPTIONS:
                if separated:
                    position += 1
                elif position + 1 < len(rest):
                    position += 2
                else:
                    return set(), [f"unsupported rebase option {name}: missing value"]
                continue
            if name in GIT_REBASE_FLAGS:
                saw_root = saw_root or name == "--root"
                position += 1
                continue
            return set(), [f"unsupported rebase option {name}"]
        if token.startswith("-") and token != "-":
            letters = token[1:]
            offset = 0
            while offset < len(letters):
                letter = letters[offset]
                if letter in GIT_REBASE_VALUE_SHORTS:
                    if offset + 1 >= len(letters):
                        if position + 1 >= len(rest):
                            return set(), [f"unsupported rebase option -{letter}: missing value"]
                        position += 1
                    offset = len(letters)
                    break
                if letter not in GIT_REBASE_FLAG_SHORTS:
                    return set(), [f"unsupported rebase option -{letter}"]
                offset += 1
            position += 1
            continue
        positionals.append(token)
        position += 1
    if modes:
        if len(modes) != 1 or positionals:
            return set(), ["unsupported rebase operation"]
        return set(), []
    if saw_root and positionals:
        return set(), ["unsupported rebase operands with --root"]
    if len(positionals) > 2:
        return set(), ["unsupported rebase operands"]
    return ({positionals[1]}, []) if len(positionals) == 2 else (set(), [])


def _fetch_operands(
    rest: list[str],
    *,
    subcommand: str,
) -> tuple[list[str], list[str]]:
    positionals: list[str] = []
    value_options = GIT_FETCH_VALUE_OPTIONS
    flags = GIT_FETCH_FLAGS
    if subcommand == "pull":
        flags = flags | GIT_PULL_EXTRA_FLAGS
        value_options = value_options | GIT_PULL_EXTRA_VALUE_OPTIONS
    position = 0
    while position < len(rest):
        token = rest[position]
        if token == "--":
            positionals.extend(rest[position + 1 :])
            break
        if token.startswith("--"):
            name, separated, _ = token.partition("=")
            if name in GIT_FETCH_UNSAFE_OPTIONS:
                return [], [f"unsupported fetch option {name}"]
            if name in value_options:
                if separated:
                    position += 1
                elif position + 1 < len(rest):
                    position += 2
                else:
                    return [], [f"unsupported fetch option {name}: missing value"]
                continue
            if name in flags:
                position += 1
                continue
            return [], [f"unsupported fetch option {name}"]
        if token.startswith("-") and token != "-":
            letters = token[1:]
            offset = 0
            while offset < len(letters):
                letter = letters[offset]
                if letter in {"j", "o", "s", "X"}:
                    if offset + 1 >= len(letters):
                        if position + 1 >= len(rest):
                            return [], [f"unsupported fetch option -{letter}: missing value"]
                        position += 1
                    offset = len(letters)
                    break
                if f"-{letter}" not in flags:
                    return [], [f"unsupported fetch option -{letter}"]
                offset += 1
            position += 1
            continue
        positionals.append(token)
        position += 1
    return positionals, []


def _refspec_issues(refspec: str, branch: str | None, protected: set[str]) -> list[str]:
    value = refspec.strip()
    if not value:
        return ["unsupported fetch refspec: empty"]
    if value.startswith("^"):
        return ["unsupported negative fetch refspec"]
    if value.startswith("+"):
        value = value[1:]
    if value.count(":") > 1:
        return ["unsupported fetch destination form"]
    if ":" not in value:
        return []
    _, destination = value.split(":", 1)
    destination = destination.strip()
    if not destination:
        return ["unsupported fetch destination form"]
    if any(character in destination for character in "*?[\\") or any(
        marker in destination for marker in ("..", "~", "^", "@{", "+")
    ):
        return ["unsupported fetch destination form"]
    normalized = _normalize_ref(destination)
    if (
        not normalized
        or destination.startswith(("/", ".", "-"))
        or destination.endswith(("/", ".", ".lock"))
        or "//" in destination
        or re.fullmatch(r"[a-z0-9._/-]+", normalized) is None
    ):
        return ["unsupported fetch destination form"]
    if normalized == "head":
        if not branch:
            return ["fetch destination HEAD has no branch context"]
        if _is_protected(branch, protected):
            return ["prohibited protected-branch mutation"]
        return []
    if _is_protected(destination, protected):
        return ["prohibited protected-branch mutation"]
    return []


def _fetch_issues(
    rest: list[str],
    branch: str | None,
    protected: set[str],
    *,
    subcommand: str,
) -> list[str]:
    positionals, issues = _fetch_operands(rest, subcommand=subcommand)
    if issues:
        return issues
    refspecs = positionals[1:]
    if subcommand == "pull" and len(positionals) == 1 and ":" in positionals[0]:
        refspecs = positionals
    for refspec in refspecs:
        issues.extend(_refspec_issues(refspec, branch, protected))
    return issues


def _branch_context_issues(
    subcommand: str,
    rest: list[str],
    branch: str | None,
    protected: set[str],
    raw: str,
) -> list[str]:
    issues: list[str] = []
    if subcommand == "reset" and "--hard" in rest:
        issues.append("prohibited hard reset")
    if not branch:
        issues.append("mutating command has no branch context")
    elif _is_protected(branch, protected):
        issues.append("prohibited mutation on protected branch")
    if (
        subcommand in {"commit", "merge", "am", "cherry-pick", "revert"}
        and "co-authored-by:" in raw.lower()
    ):
        issues.append("prohibited agent co-author trailer")
    return issues


def _git_issues(args: list[str], branch: str | None, protected: set[str], raw: str) -> list[str]:
    subcommand, rest = _split_git_global(args)
    if subcommand is None:
        return []
    names = _protected_names(protected)
    if subcommand == "push":
        return ["prohibited remote push"]
    if subcommand == "remote":
        if _first_positional(rest) in REMOTE_WRITE_ACTIONS:
            return ["prohibited remote mutation"]
        return []
    if subcommand == "branch":
        mutating = any(token in GIT_BRANCH_MUTATION_FLAGS for token in rest)
        if not mutating and any(token in GIT_BRANCH_READONLY_FLAGS for token in rest):
            return []
        targets = _positionals(rest)
        if targets and any(_is_protected(target, names) for target in targets):
            return ["prohibited protected-branch mutation"]
        return []
    if subcommand in {"checkout", "switch"}:
        target: str | None = None
        index = 0
        while index < len(rest):
            token = rest[index]
            if token == "--":
                break
            if token in GIT_CHECKOUT_CREATE_OPTIONS:
                target = rest[index + 1] if index + 1 < len(rest) else None
                break
            if token in GIT_CHECKOUT_VALUE_OPTIONS:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            target = token
            break
        if _is_protected(target, names):
            return ["prohibited protected-branch mutation"]
        return []
    if subcommand in {"update-ref", "symbolic-ref"}:
        if "--stdin" in rest:
            return ["unsupported update-ref stdin input"]
        if any(
            _is_protected(target, names)
            for target in _positionals(rest, GIT_UPDATE_REF_VALUE_OPTIONS)
        ):
            return ["prohibited protected-branch mutation"]
        return []
    if subcommand == "rebase":
        issues = _branch_context_issues(subcommand, rest, branch, names, raw)
        targets, target_issues = _rebase_targets(rest)
        issues.extend(target_issues)
        if any(_is_protected(target, names) for target in targets):
            issues.append("prohibited protected-branch mutation")
        return issues
    if subcommand in {"fetch", "pull"}:
        issues = _fetch_issues(rest, branch, names, subcommand=subcommand)
        if subcommand == "pull":
            issues.extend(_branch_context_issues(subcommand, rest, branch, names, raw))
        return issues
    if subcommand in GIT_CURRENT_BRANCH_MUTATORS:
        return _branch_context_issues(subcommand, rest, branch, names, raw)
    return []


def _gh_issues(args: list[str]) -> list[str]:
    positionals = _positionals(args, GH_GLOBAL_VALUE_OPTIONS)
    if not positionals:
        return []
    command = positionals[0]
    action = positionals[1] if len(positionals) > 1 else None
    if command == "pr" and action in GH_PR_WRITE_ACTIONS:
        return ["prohibited pull-request action"]
    if command == "release" and action in GH_RELEASE_WRITE_ACTIONS:
        return ["prohibited release publication"]
    if command == "repo" and action in GH_REPO_WRITE_ACTIONS:
        return ["prohibited remote mutation"]
    if command == "api":
        method = None
        for index, token in enumerate(args):
            if token in {"-X", "--method"} and index + 1 < len(args):
                method = args[index + 1].upper()
            elif token.startswith("--method="):
                method = token.split("=", 1)[1].upper()
            elif token.startswith("-X") and len(token) > 2:
                method = token[2:].upper()
        fields = any(
            token in {"-f", "--field", "-F", "--raw-field", "-i", "--input"}
            or token.startswith("--field=")
            for token in args
        )
        if method not in {None, "GET", "HEAD"} or (method is None and fields):
            return ["prohibited remote mutation"]
    return []


def _python_issues(args: list[str], elevated: bool) -> list[str]:
    if "-m" not in args:
        return []
    index = args.index("-m")
    module = args[index + 1] if index + 1 < len(args) else None
    rest = args[index + 2 :]
    if module == "twine" and _first_positional(rest) == "upload":
        return ["prohibited package publication"]
    if module in {"pip", "pip3"}:
        installs = _first_positional(rest) == "install"
        user_wide = any(
            token in {"--user", "--system"} or token.startswith(("--user=", "--system="))
            for token in rest
        )
        if installs and (elevated or user_wide):
            return ["prohibited global installation"]
    if module == "pipx" and _first_positional(rest) == "install":
        return ["prohibited global installation"]
    if module == "uv":
        positionals = _positionals(rest)
        if positionals[:1] == ["publish"]:
            return ["prohibited package publication"]
        if positionals[:2] == ["tool", "install"]:
            return ["prohibited global installation"]
        if positionals[:2] == ["pip", "install"] and any(
            token == "--system" or token.startswith("--system=") for token in rest
        ):
            return ["prohibited global installation"]
    return []


def _node_issues(program: str, args: list[str]) -> list[str]:
    positionals = _positionals(args)
    if not positionals:
        return []
    action = positionals[0]
    issues: list[str] = []
    if action in {"publish", "unpublish"}:
        issues.append("prohibited package publication")
    global_flag = any(
        token in {"-g", "--global"} or token.startswith("--location=global") for token in args
    )
    if action in {"install", "i", "add"} and global_flag:
        issues.append("prohibited global installation")
    if program == "yarn" and action == "global" and "add" in positionals[1:]:
        issues.append("prohibited global installation")
    return issues


def _shell_wrapper_issues(
    program: str,
    args: list[str],
    branch: str | None,
    protected: set[str],
    raw: str,
    depth: int,
) -> list[str]:
    payload: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            if "c" in token[1:]:
                if index + 1 >= len(args):
                    return [f"{program} -c wrapper has no payload"]
                payload = args[index + 1]
                break
            index += 1
            continue
        break
    if payload is None:
        return [f"unsupported {program} wrapper without a readable -c payload"]
    if depth >= SHELL_DEPTH_LIMIT:
        return ["nested shell wrapper exceeds the audit depth limit"]
    if "$" in payload:
        return ["shell wrapper payload is not statically readable (variable expansion)"]
    try:
        segments = _shell_segments(payload)
    except ValueError as exc:
        return [f"shell wrapper payload cannot be audited safely: {exc}"]
    return _segment_issues(segments, branch, protected, raw, depth + 1)


def _segment_issues(
    segments: list[list[str]],
    branch: str | None,
    protected: set[str],
    raw: str,
    depth: int,
) -> list[str]:
    issues: list[str] = []
    for tokens in segments:
        if any(_program_name(token) == "env" for token in tokens) and any(
            token in {"-S", "--split-string"} or token.startswith(("-S", "--split-string="))
            for token in tokens
        ):
            issues.append("unsupported env split-string wrapper")
            continue
        tokens, elevated = _strip_prefixes(tokens)
        if not tokens:
            issues.append("command has no executable")
            continue
        program = _program_name(tokens[0])
        args = tokens[1:]
        if program == "eval":
            issues.append("prohibited dynamic evaluation")
            continue
        if program in SHELL_CONTROL_KEYWORDS:
            issues.append("unsupported shell control syntax")
            continue
        if program in SHELL_PROGRAMS:
            issues.extend(_shell_wrapper_issues(program, args, branch, protected, raw, depth))
            continue
        if program == "git":
            issues.extend(_git_issues(args, branch, protected, raw))
        elif PYTHON_PROGRAM.fullmatch(program):
            issues.extend(_python_issues(args, elevated))
        elif program == "gh":
            issues.extend(_gh_issues(args))
        elif program == "hub":
            if _first_positional(args) == "pull-request":
                issues.append("prohibited pull-request action")
        elif program == "twine":
            if _first_positional(args) == "upload":
                issues.append("prohibited package publication")
        elif program in {"pip", "pip3"}:
            if _first_positional(args) == "install" and (
                elevated
                or any(
                    token in {"--user", "--system"} or token.startswith(("--user=", "--system="))
                    for token in args
                )
            ):
                issues.append("prohibited global installation")
        elif program == "pipx":
            if _first_positional(args) == "install":
                issues.append("prohibited global installation")
        elif program == "uv":
            positionals = _positionals(args)
            if positionals[:1] == ["publish"]:
                issues.append("prohibited package publication")
            if positionals[:2] == ["tool", "install"]:
                issues.append("prohibited global installation")
            if positionals[:2] == ["pip", "install"] and (
                elevated or any(token == "--system" for token in args)
            ):
                issues.append("prohibited global installation")
        elif program in {"npm", "pnpm", "yarn", "bun"}:
            issues.extend(_node_issues(program, args))
        elif program in {"cargo", "poetry", "hatch", "flit"}:
            if _first_positional(args) == "publish":
                issues.append("prohibited package publication")
        elif program == "gem":
            action = _first_positional(args)
            if action == "push":
                issues.append("prohibited package publication")
            elif action == "install":
                issues.append("prohibited global installation")
        elif program in {"docker", "podman", "nerdctl", "docker-compose", "podman-compose"}:
            action = _first_positional(args)
            if action is None or action not in CONTAINER_READONLY_ACTIONS:
                issues.append("prohibited container operation")
    return issues


def _command_issues(command: str, branch: str | None, protected: set[str]) -> list[str]:
    """Independently flag prohibited commands in the recorded action log."""
    try:
        segments = _shell_segments(command)
    except ValueError as exc:
        issues = [f"command cannot be audited safely: {exc}"]
        if INSTRUCTION_FILE_WRITE.search(command):
            issues.append("prohibited instruction-file edit")
        return issues
    issues = _segment_issues(segments, branch, protected, command, 0)
    if INSTRUCTION_FILE_WRITE.search(command):
        issues.append("prohibited instruction-file edit")
    return issues


def _audit_delivery(root: Path) -> list[str]:
    """Independently audit AC-45 recorded refs, action log and handoff."""
    issues: list[str] = []
    manifest = _read_json(root / "manifest.json")
    refs = _read_json(root / "refs.json")
    handoff = (root / "handoff.md").read_text(encoding="utf-8")

    initial = refs["initial_commit"]
    if not HEX40.fullmatch(initial):
        issues.append("initial commit is not a full commit id")
    if refs["base_ref_commit"] != initial:
        issues.append("base ref moved from the initial commit")
    if refs["default_ref_commit"] != initial:
        issues.append("default ref moved from the initial commit")
    protected = refs.get("protected_refs", {})
    if not protected:
        issues.append("no protected refs recorded")
    for name, commit in protected.items():
        if commit != initial:
            issues.append(f"protected branch {name} was mutated")
    if refs["default_ref"] not in protected:
        issues.append("default ref is not recorded as protected")
    branch = refs["feature_branch"]
    if branch in DEFAULT_BRANCHES or branch in {refs["base_ref"], refs["default_ref"]}:
        issues.append("feature branch is a default or protected branch")
    if branch in protected:
        issues.append("feature branch is protected")
    if refs["feature_commit"] != manifest["tested_commit"]:
        issues.append("feature commit differs from tested commit")
    if refs["feature_tree"] != manifest["tested_tree"]:
        issues.append("feature tree differs from tested tree")
    if refs["descends_from_initial"] is not True:
        issues.append("feature branch does not descend from the initial commit")
    if refs["default_merges"]:
        issues.append("a default-branch merge is recorded")

    entries = []
    for line in (root / "action_log.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    if not entries:
        issues.append("action log is empty")
    sequences: set[int] = set()
    for entry in entries:
        seq = entry.get("seq")
        if not isinstance(seq, int) or seq < 1 or seq in sequences:
            issues.append(f"invalid or duplicate action seq {seq!r}")
            continue
        sequences.add(seq)
        command = entry.get("command", "")
        for issue in _command_issues(command, entry.get("branch"), set(protected)):
            issues.append(f"{issue}: {command}")

    if _field(handoff, "Feature branch") is None:
        issues.append("handoff feature branch is missing")
    if _field(handoff, "Final commit") is None:
        issues.append("handoff final commit is missing")
    if _field(handoff, "Initial base") is None:
        issues.append("handoff initial base is missing")
    if _first_hex(_field(handoff, "Final commit")) != manifest["tested_commit"]:
        issues.append("handoff final commit differs from tested commit")
    base = _first_hex(_field(handoff, "Initial base"))
    if base is None or not manifest["initial_commit"].startswith(base):
        issues.append("handoff initial base differs from the recorded base")

    changed = _section(handoff, "Changed tracked files")
    if changed is None:
        issues.append("handoff changed files section is missing")
    else:
        listed = {
            match.strip()
            for bullet in re.findall(r"^\s*[-*]\s+(.+)$", changed, re.MULTILINE)
            for match in re.findall(r"`([^`]+)`", bullet)
        }
        for path in manifest["changed_files"]:
            if path not in listed:
                issues.append(f"handoff does not list changed file {path}")

    validations = _section(handoff, "Validations")
    if validations is None:
        issues.append("handoff validations section is missing")
    else:
        for gate in manifest["gates"]:
            if gate["id"] == "AC-43":
                continue
            line = next(
                (item for item in validations.splitlines() if gate["id"] in item),
                None,
            )
            if line is None or gate["log"] not in line or "pass" not in line.lower():
                issues.append(f"handoff validation for {gate['id']} lacks its evidence path")

    synthesis = _section(handoff, "Review and QA synthesis")
    if synthesis is None:
        issues.append("handoff synthesis section is missing")
    else:
        for key, path in manifest["reports"].items():
            if path not in synthesis:
                issues.append(f"handoff synthesis does not reference {key}")

    unmet = _section(handoff, "Unmet requirements")
    if unmet is None:
        issues.append("handoff unmet requirements section is missing")
    elif not manifest["unmet_requirements"] and not re.search(r"\bnone\b", unmet, re.IGNORECASE):
        issues.append("handoff does not state that there are no unmet requirements")
    for item in manifest["unmet_requirements"]:
        if item["id"] not in (unmet or ""):
            issues.append(f"handoff does not acknowledge unmet requirement {item['id']}")

    lowered = handoff.lower()
    if HANDOFF_PHRASE not in lowered:
        issues.append("handoff omits the no-live-calls statement")
    if REMOTE_SCOPE not in lowered:
        issues.append("handoff omits the remote-action audit scope")
    return issues


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- fixture mutations (each runs only against a tmp copy) -----------------


def _mutate_base_ref(root: Path) -> None:
    refs = _refs(root)
    refs["base_ref_commit"] = "f" * 40
    _write_json(root / "refs.json", refs)


def _mutate_default_ref(root: Path) -> None:
    refs = _refs(root)
    refs["default_ref_commit"] = "f" * 40
    _write_json(root / "refs.json", refs)


def _mutate_protected_ref(root: Path) -> None:
    refs = _refs(root)
    refs["protected_refs"]["main"] = "f" * 40
    _write_json(root / "refs.json", refs)


def _mutate_feature_branch_default(root: Path) -> None:
    refs = _refs(root)
    refs["feature_branch"] = "main"
    _write_json(root / "refs.json", refs)


def _mutate_no_descend(root: Path) -> None:
    refs = _refs(root)
    refs["descends_from_initial"] = False
    _write_json(root / "refs.json", refs)


def _mutate_default_merge(root: Path) -> None:
    refs = _refs(root)
    refs["default_merges"] = [{"into": "main", "commit": "f" * 40}]
    _write_json(root / "refs.json", refs)


def _mutate_push(root: Path) -> None:
    _append_action(root, "git push origin feature/b11c205b-deepsearch-axi-cli")


def _mutate_protected_commit(root: Path) -> None:
    _append_action(root, "git commit -m 'direct edit'", branch="main")


def _mutate_twine_upload(root: Path) -> None:
    _append_action(root, "python -m twine upload dist/*")


def _mutate_gh_pr_create(root: Path) -> None:
    _append_action(root, "gh pr create --title evidence --body synthetic")


def _mutate_docker_run(root: Path) -> None:
    _append_action(root, "docker run --rm alpine true")


def _mutate_dash_c_push(root: Path) -> None:
    _append_action(root, "git -C /owned/repo push origin main")


def _mutate_global_option_push(root: Path) -> None:
    _append_action(root, "git --git-dir=/owned/repo/.git push origin main")


def _mutate_config_push(root: Path) -> None:
    _append_action(root, "git -c core.hooksPath=/dev/null push origin main")


def _mutate_force_protected_branch(root: Path) -> None:
    _append_action(
        root,
        "git branch -f main HEAD",
        branch="feature/b11c205b-deepsearch-axi-cli",
    )


def _mutate_update_ref_protected(root: Path) -> None:
    _append_action(root, "git update-ref refs/heads/main deadbeef")


def _mutate_checkout_default(root: Path) -> None:
    _append_action(root, "git checkout main")


def _mutate_gh_release(root: Path) -> None:
    _append_action(root, "gh release create v0.1.0")


def _mutate_npm_publish(root: Path) -> None:
    _append_action(root, "npm publish")


def _mutate_npm_global_install(root: Path) -> None:
    _append_action(root, "npm install -g deepsearch-axi")


def _mutate_pip_user_install(root: Path) -> None:
    _append_action(root, "python -m pip install --user deepsearch-axi")


def _mutate_uv_tool_install(root: Path) -> None:
    _append_action(root, "uv tool install deepsearch-axi")


def _mutate_remote_add(root: Path) -> None:
    _append_action(root, "git remote add origin git@example.com:ddbaron/deepsearch-axi.git")


def _mutate_coauthor_trailer(root: Path) -> None:
    _append_action(
        root,
        "git commit -m 'add evidence\n\nCo-Authored-By: Agent <agent@example.com>'",
    )


def _mutate_instruction_file(root: Path) -> None:
    _append_action(root, "printf 'x\\n' > AGENTS.md")


def _mutate_rebase_explicit_target(root: Path) -> None:
    _append_action(
        root,
        f"git rebase {FEATURE_BRANCH} main",
        branch=FEATURE_BRANCH,
    )


def _mutate_fetch_destination(root: Path) -> None:
    _append_action(root, "git fetch . HEAD:refs/heads/main")


def _mutate_force_fetch_destination(root: Path) -> None:
    _append_action(root, "git fetch . +HEAD:refs/heads/main")


def _mutate_multiple_fetch_destinations(root: Path) -> None:
    _append_action(
        root,
        "git fetch . HEAD:refs/heads/feature HEAD:refs/heads/main",
    )


def _mutate_shell_composed_push(root: Path) -> None:
    _append_action(root, "git status --short && git push origin main")


def _mutate_shell_wrapper_push(root: Path) -> None:
    _append_action(root, "sh -c 'git push origin main'")


def _mutate_shell_wrapper_rebase_target(root: Path) -> None:
    _append_action(
        root,
        f"bash -c 'git rebase {FEATURE_BRANCH} main'",
        branch=FEATURE_BRANCH,
    )


def _mutate_handoff_branch(root: Path) -> None:
    path = root / "handoff.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r"^- Feature branch:.*\n", "", text, flags=re.MULTILINE), encoding="utf-8"
    )


def _mutate_handoff_commit(root: Path) -> None:
    path = root / "handoff.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "- Final commit: `0123456789abcdef0123456789abcdef01234567`",
        "- Final commit: `ffffffffffffffffffffffffffffffffffffffff`",
    )
    path.write_text(text, encoding="utf-8")


def _mutate_handoff_changed_files(root: Path) -> None:
    path = root / "handoff.md"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^## Changed tracked files.*?(?=^## )", "", text, flags=re.MULTILINE | re.DOTALL)
    path.write_text(text, encoding="utf-8")


def _mutate_handoff_validation(root: Path) -> None:
    path = root / "handoff.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"^- AC-44:.*\n", "", text, flags=re.MULTILINE), encoding="utf-8")


def _mutate_handoff_unmet(root: Path) -> None:
    path = root / "handoff.md"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^## Unmet requirements.*?(?=^## |\Z)", "", text, flags=re.MULTILINE | re.DOTALL)
    path.write_text(text, encoding="utf-8")


def _mutate_handoff_phrase(root: Path) -> None:
    path = root / "handoff.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(
            r"no live API calls performed; managing crewmate acceptance pending\.?",
            "acceptance pending.",
            text,
            flags=re.IGNORECASE,
        ),
        encoding="utf-8",
    )


def _mutate_handoff_scope(root: Path) -> None:
    path = root / "handoff.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("they cannot prove absence of remote actions", "the audit is complete"),
        encoding="utf-8",
    )


DELIVERY_MUTATIONS = [
    pytest.param(_mutate_base_ref, "base ref", id="base-ref-moved"),
    pytest.param(_mutate_default_ref, "default ref", id="default-ref-moved"),
    pytest.param(_mutate_protected_ref, "protected branch", id="protected-ref-mutated"),
    pytest.param(_mutate_feature_branch_default, "default or protected", id="feature-is-main"),
    pytest.param(_mutate_no_descend, "descend", id="branch-does-not-descend"),
    pytest.param(_mutate_default_merge, "default-branch merge", id="default-merge-recorded"),
    pytest.param(_mutate_push, "push", id="prohibited-push"),
    pytest.param(_mutate_dash_c_push, "remote push", id="prohibited-dash-c-push"),
    pytest.param(_mutate_global_option_push, "remote push", id="prohibited-global-option-push"),
    pytest.param(_mutate_config_push, "remote push", id="prohibited-config-push"),
    pytest.param(_mutate_gh_pr_create, "pull-request", id="prohibited-gh-pr-create"),
    pytest.param(_mutate_gh_release, "release", id="prohibited-gh-release"),
    pytest.param(_mutate_docker_run, "container", id="prohibited-docker-run"),
    pytest.param(_mutate_npm_publish, "package publication", id="prohibited-npm-publish"),
    pytest.param(
        _mutate_npm_global_install,
        "global installation",
        id="prohibited-npm-global-install",
    ),
    pytest.param(
        _mutate_pip_user_install,
        "global installation",
        id="prohibited-pip-user-install",
    ),
    pytest.param(
        _mutate_uv_tool_install,
        "global installation",
        id="prohibited-uv-tool-install",
    ),
    pytest.param(_mutate_remote_add, "remote mutation", id="prohibited-remote-add"),
    pytest.param(
        _mutate_force_protected_branch,
        "protected-branch",
        id="prohibited-force-protected-branch",
    ),
    pytest.param(
        _mutate_update_ref_protected,
        "protected-branch",
        id="prohibited-update-ref-protected",
    ),
    pytest.param(
        _mutate_checkout_default,
        "protected-branch",
        id="prohibited-checkout-default",
    ),
    pytest.param(
        _mutate_coauthor_trailer,
        "co-author",
        id="prohibited-coauthor-trailer",
    ),
    pytest.param(
        _mutate_instruction_file,
        "instruction-file",
        id="prohibited-instruction-file-edit",
    ),
    pytest.param(_mutate_protected_commit, "protected branch", id="protected-commit"),
    pytest.param(_mutate_twine_upload, "package publication", id="prohibited-upload"),
    pytest.param(_mutate_handoff_branch, "feature branch is missing", id="handoff-branch-missing"),
    pytest.param(_mutate_handoff_commit, "final commit", id="handoff-commit-mismatch"),
    pytest.param(
        _mutate_handoff_changed_files,
        "changed files section is missing",
        id="handoff-changed-files-missing",
    ),
    pytest.param(
        _mutate_handoff_validation,
        "validation for AC-44",
        id="handoff-validation-missing",
    ),
    pytest.param(
        _mutate_handoff_unmet,
        "unmet requirements section is missing",
        id="handoff-unmet-missing",
    ),
    pytest.param(
        _mutate_handoff_phrase,
        "no-live-calls",
        id="handoff-required-phrase-missing",
    ),
    pytest.param(
        _mutate_handoff_scope,
        "remote-action audit scope",
        id="handoff-audit-scope-missing",
    ),
]


# --- explicit Git mutation targets -----------------------------------------
#
# Expected outcomes below are written by hand from the acceptance contract, not
# derived from either auditor. Every row runs against both implementations.

GIT_TARGET_CASES = [
    pytest.param(
        f"git rebase {FEATURE_BRANCH} main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="rebase-explicit-main",
    ),
    pytest.param(
        f"git rebase --onto main {FEATURE_BRANCH} main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="rebase-onto-explicit-main",
    ),
    pytest.param(
        f"git rebase -i {FEATURE_BRANCH} main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="rebase-interactive-explicit-main",
    ),
    pytest.param(
        f"git rebase refs/heads/{FEATURE_BRANCH} refs/heads/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="rebase-fully-qualified-main",
    ),
    pytest.param(
        f"git rebase {FEATURE_BRANCH} origin/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="rebase-remote-spelling-main",
    ),
    pytest.param(
        f"git rebase {FEATURE_BRANCH} MAIN",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="rebase-case-variant-main",
    ),
    pytest.param(
        f"git rebase {FEATURE_BRANCH} ' main '",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="rebase-whitespace-variant-main",
    ),
    pytest.param(
        f"git rebase --unknown-option {FEATURE_BRANCH} main",
        FEATURE_BRANCH,
        True,
        "unsupported rebase option",
        id="rebase-unknown-option-fails-closed",
    ),
    pytest.param(
        "git rebase --onto",
        FEATURE_BRANCH,
        True,
        "unsupported rebase option",
        id="rebase-missing-option-value-fails-closed",
    ),
    pytest.param(
        f"git rebase {FEATURE_BRANCH} main extra",
        FEATURE_BRANCH,
        True,
        "unsupported rebase operands",
        id="rebase-extra-operands-fail-closed",
    ),
    pytest.param(
        "git rebase main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-rebase-upstream-only",
    ),
    pytest.param(
        "git rebase origin/main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-rebase-remote-upstream",
    ),
    pytest.param(
        f"git rebase main {FEATURE_BRANCH}",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-rebase-feature-target",
    ),
    pytest.param(
        f"git rebase --onto main {FEATURE_BRANCH}",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-rebase-onto-with-upstream",
    ),
    pytest.param(
        "git rebase -i main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-interactive-rebase-upstream-only",
    ),
    pytest.param(
        "git rebase --continue",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-rebase-continue",
    ),
    pytest.param(
        "git rebase --abort",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-rebase-abort",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-destination-main-feature-context",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/main",
        "main",
        True,
        "protected-branch",
        id="fetch-destination-main-protected-context",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/main",
        None,
        True,
        "protected-branch",
        id="fetch-destination-main-no-context",
    ),
    pytest.param(
        "git fetch . +HEAD:refs/heads/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-forced-destination-main",
    ),
    pytest.param(
        "git fetch . HEAD:main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-short-destination-main",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/MAIN",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-case-variant-destination",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/origin/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-origin-spelling-destination",
    ),
    pytest.param(
        "git fetch . :refs/heads/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-deletion-destination-main",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/main~1",
        FEATURE_BRANCH,
        True,
        "unsupported fetch destination",
        id="fetch-revision-destination-fails-closed",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/main:extra",
        FEATURE_BRANCH,
        True,
        "unsupported fetch destination",
        id="fetch-multiple-colon-destination-fails-closed",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/",
        FEATURE_BRANCH,
        True,
        "unsupported fetch destination",
        id="fetch-empty-ref-destination-fails-closed",
    ),
    pytest.param(
        "git fetch . HEAD:main.",
        FEATURE_BRANCH,
        True,
        "unsupported fetch destination",
        id="fetch-trailing-dot-destination-fails-closed",
    ),
    pytest.param(
        "git fetch . HEAD://main",
        FEATURE_BRANCH,
        True,
        "unsupported fetch destination",
        id="fetch-leading-slash-destination-fails-closed",
    ),
    pytest.param(
        "git fetch . HEAD:main.lock",
        FEATURE_BRANCH,
        True,
        "unsupported fetch destination",
        id="fetch-lockfile-destination-fails-closed",
    ),
    pytest.param(
        "git fetch . HEAD:HEAD",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-fetch-head-destination-on-feature",
    ),
    pytest.param(
        "git fetch . HEAD:HEAD",
        "main",
        True,
        "protected-branch",
        id="reject-fetch-head-destination-on-main",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/main HEAD:refs/heads/feature",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-multiple-refspecs-include-main",
    ),
    pytest.param(
        "git fetch --refmap=+refs/heads/*:refs/heads/main origin",
        FEATURE_BRANCH,
        True,
        "unsupported fetch option",
        id="fetch-refmap-fails-closed",
    ),
    pytest.param(
        "git fetch --mirror origin",
        FEATURE_BRANCH,
        True,
        "unsupported fetch option",
        id="fetch-mirror-fails-closed",
    ),
    pytest.param(
        "git fetch origin main:main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-source-to-main",
    ),
    pytest.param(
        "git fetch --depth 1 origin main:main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="fetch-option-value-not-a-refspec",
    ),
    pytest.param(
        "git pull . HEAD:refs/heads/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="pull-destination-main",
    ),
    pytest.param(
        "git pull origin main:refs/heads/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="pull-source-to-main",
    ),
    pytest.param(
        "git fetch origin main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-fetch-source-only",
    ),
    pytest.param(
        "git fetch . HEAD:refs/heads/feature",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-fetch-feature-destination",
    ),
    pytest.param(
        "git fetch --depth 1 origin main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-fetch-depth-source-only",
    ),
    pytest.param("git fetch --all", FEATURE_BRANCH, False, "", id="allow-fetch-all"),
    pytest.param("git fetch origin", FEATURE_BRANCH, False, "", id="allow-fetch-remote"),
    pytest.param(
        "git pull origin main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-pull-source-only",
    ),
    pytest.param(
        "git pull --rebase origin main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-pull-rebase-source-only",
    ),
    pytest.param("git pull origin", FEATURE_BRANCH, False, "", id="allow-pull-remote"),
    pytest.param(
        "git push origin main",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-push",
    ),
    pytest.param(
        "git branch -f main HEAD",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="reject-force-branch-main",
    ),
    pytest.param(
        "git update-ref refs/heads/main deadbeef",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="reject-update-ref-main",
    ),
    pytest.param(
        "git checkout main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="reject-checkout-main",
    ),
    pytest.param(
        "git switch -c main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="reject-switch-create-main",
    ),
    pytest.param(
        "git checkout --conflict merge main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="reject-checkout-option-value-then-main",
    ),
    pytest.param(
        "git reset --hard HEAD",
        FEATURE_BRANCH,
        True,
        "hard reset",
        id="reject-hard-reset",
    ),
    pytest.param(
        "git commit -m x",
        "main",
        True,
        "protected branch",
        id="reject-commit-on-main",
    ),
    pytest.param(
        "git merge main",
        "main",
        True,
        "protected branch",
        id="reject-merge-on-main",
    ),
    pytest.param(
        "git rebase main",
        "main",
        True,
        "protected branch",
        id="reject-rebase-on-main",
    ),
    pytest.param(
        "git commit -m 'x\n\nCo-Authored-By: Agent <agent@example.com>'",
        FEATURE_BRANCH,
        True,
        "co-author",
        id="reject-coauthor-trailer",
    ),
    pytest.param("git log main", FEATURE_BRANCH, False, "", id="allow-log-main"),
    pytest.param(
        "git show refs/heads/main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-show-main",
    ),
    pytest.param(
        "git diff main...HEAD",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-diff-main",
    ),
    pytest.param(
        "git rev-parse main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-rev-parse-main",
    ),
    pytest.param(
        "git branch --show-current",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-branch-show-current",
    ),
    pytest.param(
        "git branch --list main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-branch-list-main",
    ),
    pytest.param(
        f"git checkout {FEATURE_BRANCH}",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-checkout-feature",
    ),
    pytest.param(
        "git commit -m x",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-commit-on-feature",
    ),
    pytest.param(
        "git merge main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-merge-source-main",
    ),
    pytest.param(
        "git reset HEAD",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-soft-reset",
    ),
    pytest.param(
        "git update-ref refs/heads/feature/b11c205b-deepsearch-axi-cli deadbeef",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-update-ref-feature",
    ),
    pytest.param(
        "git status --porcelain",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-status",
    ),
]


def _deep_shell_wrapper() -> str:
    command = "git status"
    for _ in range(SHELL_DEPTH_LIMIT + 1):
        command = "sh -c " + shlex.quote(command)
    return command


SHELL_COMPOSITION_CASES = [
    pytest.param(
        "git status --short && git push origin main",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-and-composed-push",
    ),
    pytest.param(
        "git status --short || git push origin main",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-or-composed-push",
    ),
    pytest.param(
        "git status; git push origin main",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-semicolon-composed-push",
    ),
    pytest.param(
        "git status | git push origin main",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-pipe-composed-push",
    ),
    pytest.param(
        "git status\ngit push origin main",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-newline-composed-push",
    ),
    pytest.param(
        "git status --short && git fetch . HEAD:refs/heads/main",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="reject-and-composed-fetch-destination",
    ),
    pytest.param(
        "sh -c 'git push origin main'",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-shell-wrapper-push",
    ),
    pytest.param(
        'bash -c "git push origin main"',
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-bash-wrapper-push",
    ),
    pytest.param(
        "zsh -c \"sh -c 'git push origin main'\"",
        FEATURE_BRANCH,
        True,
        "remote push",
        id="reject-nested-shell-wrapper-push",
    ),
    pytest.param(
        _deep_shell_wrapper(),
        FEATURE_BRANCH,
        True,
        "depth limit",
        id="reject-shell-wrapper-beyond-depth",
    ),
    pytest.param(
        "sh -c 'git rebase feature/b11c205b-deepsearch-axi-cli main'",
        FEATURE_BRANCH,
        True,
        "protected-branch",
        id="reject-shell-wrapper-rebase-target",
    ),
    pytest.param(
        "sh script.sh",
        FEATURE_BRANCH,
        True,
        "wrapper",
        id="reject-shell-wrapper-without-payload",
    ),
    pytest.param(
        "git status &&",
        FEATURE_BRANCH,
        True,
        "empty shell segment",
        id="reject-trailing-operator",
    ),
    pytest.param(
        "git status ;; git status",
        FEATURE_BRANCH,
        True,
        "empty shell segment",
        id="reject-empty-segment",
    ),
    pytest.param(
        "eval 'git push origin main'",
        FEATURE_BRANCH,
        True,
        "dynamic evaluation",
        id="reject-eval",
    ),
    pytest.param(
        "git commit -m `date`",
        FEATURE_BRANCH,
        True,
        "substitution",
        id="reject-backtick-substitution",
    ),
    pytest.param(
        'git commit -m "fix $(date)"',
        FEATURE_BRANCH,
        True,
        "substitution",
        id="reject-dollar-paren-substitution",
    ),
    pytest.param(
        "git status &",
        FEATURE_BRANCH,
        True,
        "background",
        id="reject-background",
    ),
    pytest.param(
        "git status > out.txt",
        FEATURE_BRANCH,
        True,
        "redirect",
        id="reject-redirect",
    ),
    pytest.param(
        "cat <<EOF",
        FEATURE_BRANCH,
        True,
        "redirect",
        id="reject-heredoc",
    ),
    pytest.param(
        "(git push origin main)",
        FEATURE_BRANCH,
        True,
        "subshell",
        id="reject-subshell-grouping",
    ),
    pytest.param(
        "! git push origin main",
        FEATURE_BRANCH,
        True,
        "control",
        id="reject-negation-control",
    ),
    pytest.param(
        "{ git status; }",
        FEATURE_BRANCH,
        True,
        "control",
        id="reject-brace-grouping",
    ),
    pytest.param(
        "if true; then git status; fi",
        FEATURE_BRANCH,
        True,
        "control",
        id="reject-if-control",
    ),
    pytest.param(
        "sh -c '$CMD'",
        FEATURE_BRANCH,
        True,
        "statically readable",
        id="reject-shell-wrapper-variable-payload",
    ),
    pytest.param(
        "env -S \"sh -c 'git push origin main'\"",
        FEATURE_BRANCH,
        True,
        "split-string",
        id="reject-env-split-string-wrapper",
    ),
    pytest.param(
        "sudo -E env -S \"sh -c 'git push origin main'\"",
        FEATURE_BRANCH,
        True,
        "split-string",
        id="reject-nested-env-split-string-wrapper",
    ),
    pytest.param(
        "builtin eval 'git push origin main'",
        FEATURE_BRANCH,
        True,
        "dynamic evaluation",
        id="reject-builtin-eval",
    ),
    pytest.param(
        "git status --short && git status",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-safe-compound",
    ),
    pytest.param(
        "git status && git log main",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-compound-read-only-main",
    ),
    pytest.param(
        "echo 'git push origin main'",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-quoted-literal-command",
    ),
    pytest.param(
        'echo "a && b"',
        FEATURE_BRANCH,
        False,
        "",
        id="allow-quoted-operator",
    ),
    pytest.param(
        "git commit -m 'fix $(date)'",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-single-quoted-substitution",
    ),
    pytest.param(
        'git commit -m "fix \\"quoted\\" words"',
        FEATURE_BRANCH,
        False,
        "",
        id="allow-double-quoted-escaped-quotes",
    ),
    pytest.param(
        'git commit -m "fix \\"$(date)\\""',
        FEATURE_BRANCH,
        True,
        "substitution",
        id="reject-escaped-quote-substitution",
    ),
    pytest.param(
        "git -C /owned/repo status",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-git-global-option",
    ),
    pytest.param(
        "python -m pytest -q",
        FEATURE_BRANCH,
        False,
        "",
        id="allow-python-test-command",
    ),
]

EXPLICIT_TARGET_MUTATIONS = [
    pytest.param(_mutate_rebase_explicit_target, "protected-branch", id="rebase-explicit-main"),
    pytest.param(_mutate_fetch_destination, "protected-branch", id="fetch-destination-main"),
    pytest.param(
        _mutate_force_fetch_destination,
        "protected-branch",
        id="fetch-forced-destination-main",
    ),
    pytest.param(
        _mutate_multiple_fetch_destinations,
        "protected-branch",
        id="fetch-multiple-destinations",
    ),
    pytest.param(_mutate_shell_composed_push, "remote push", id="shell-composed-push"),
    pytest.param(_mutate_shell_wrapper_push, "remote push", id="shell-wrapper-push"),
    pytest.param(
        _mutate_shell_wrapper_rebase_target,
        "protected-branch",
        id="shell-wrapper-rebase-target",
    ),
]


# --- verifier mutations ----------------------------------------------------


def _drop_gate(root: Path) -> None:
    manifest = _manifest(root)
    manifest["gates"] = [gate for gate in manifest["gates"] if gate["id"] != "AC-17"]
    _write_json(root / "manifest.json", manifest)


def _fail_gate(root: Path) -> None:
    manifest = _manifest(root)
    gate = next(item for item in manifest["gates"] if item["id"] == "AC-17")
    gate["status"] = "failed"
    gate["exit_code"] = 1
    _write_json(root / "manifest.json", manifest)


def _skip_gate(root: Path) -> None:
    manifest = _manifest(root)
    gate = next(item for item in manifest["gates"] if item["id"] == "AC-17")
    gate["status"] = "skipped"
    gate["exit_code"] = None
    _write_json(root / "manifest.json", manifest)


def _delete_log(root: Path) -> None:
    (root / "logs" / "AC-17.log").unlink()


def _empty_log(root: Path) -> None:
    (root / "logs" / "AC-17.log").write_text("\n  \n", encoding="utf-8")


def _add_unknown_gate(root: Path) -> None:
    manifest = _manifest(root)
    manifest["gates"].append(
        {
            "id": "AC-99",
            "command": "python scripts/not_a_gate.py",
            "status": "passed",
            "exit_code": 0,
            "log": "logs/AC-01.log",
            "summary": "unknown gate added by a mutation test",
            "tested_commit": manifest["tested_commit"],
            "tested_tree": manifest["tested_tree"],
        }
    )
    _write_json(root / "manifest.json", manifest)


def _change_commit(root: Path) -> None:
    manifest = _manifest(root)
    manifest["tested_commit"] = "f" * 40
    _write_json(root / "manifest.json", manifest)


def _change_initial_commit(root: Path) -> None:
    manifest = _manifest(root)
    manifest["initial_commit"] = "f" * 40
    _write_json(root / "manifest.json", manifest)


def _change_tree(root: Path) -> None:
    manifest = _manifest(root)
    manifest["tested_tree"] = "e" * 40
    _write_json(root / "manifest.json", manifest)


def _delete_gate_source_identity(root: Path) -> None:
    manifest = _manifest(root)
    for gate in manifest["gates"]:
        gate.pop("tested_commit", None)
        gate.pop("tested_tree", None)
    _write_json(root / "manifest.json", manifest)


def _null_gate_source_identity(root: Path) -> None:
    manifest = _manifest(root)
    for gate in manifest["gates"]:
        gate["tested_commit"] = None
        gate["tested_tree"] = None
    _write_json(root / "manifest.json", manifest)


def _wrong_gate_source_commit(root: Path) -> None:
    manifest = _manifest(root)
    gate = next(item for item in manifest["gates"] if item["id"] == "AC-01")
    gate["tested_commit"] = "f" * 40
    _write_json(root / "manifest.json", manifest)


def _wrong_gate_source_tree(root: Path) -> None:
    manifest = _manifest(root)
    gate = next(item for item in manifest["gates"] if item["id"] == "AC-01")
    gate["tested_tree"] = "e" * 40
    _write_json(root / "manifest.json", manifest)


def _wrong_gate_command(root: Path) -> None:
    manifest = _manifest(root)
    gate = next(item for item in manifest["gates"] if item["id"] == "AC-01")
    gate["command"] = "python -c 'print(1)'"
    _write_json(root / "manifest.json", manifest)


def _weakened_gate_command(root: Path) -> None:
    manifest = _manifest(root)
    gate = next(item for item in manifest["gates"] if item["id"] == "AC-01")
    gate["command"] = "python -m pytest -q"
    _write_json(root / "manifest.json", manifest)


def _extended_gate_command(root: Path) -> None:
    manifest = _manifest(root)
    gate = next(item for item in manifest["gates"] if item["id"] == "AC-01")
    gate["command"] = gate["command"] + " --ignore=tests/test_packaging.py"
    _write_json(root / "manifest.json", manifest)


def _set_finding_open(root: Path, report_name: str, finding_id: str) -> None:
    report_path = root / "reports" / report_name
    report = _read_json(report_path)
    finding = next(item for item in report["findings"] if item["id"] == finding_id)
    finding["status"] = "open"
    finding.pop("resolution", None)
    _write_json(report_path, report)
    synthesis_path = root / "reports" / "synthesis.json"
    synthesis = _read_json(synthesis_path)
    synthesis["status"] = "open"
    synthesis["resolved_findings"] = [
        item for item in synthesis["resolved_findings"] if item != finding_id
    ]
    synthesis["open_findings"] = [finding_id]
    synthesis["summary"] = "Synthesis is blocked because a finding remains unresolved."
    _write_json(synthesis_path, synthesis)


def _open_correctness_finding(root: Path) -> None:
    _set_finding_open(root, "review.json", "REV-1")


def _open_security_finding(root: Path) -> None:
    _set_finding_open(root, "qa.json", "QA-1")


def _bare_approval(root: Path) -> None:
    _write_json(
        root / "reports" / "review.json",
        {
            "role": "independent-review",
            "reviewer": "reviewer-alpha",
            "verdict": "approved",
        },
    )


def _same_reviewer(root: Path) -> None:
    report_path = root / "reports" / "qa.json"
    report = _read_json(report_path)
    report["reviewer"] = "reviewer-alpha"
    _write_json(report_path, report)


def _same_role(root: Path) -> None:
    report_path = root / "reports" / "qa.json"
    report = _read_json(report_path)
    report["role"] = "independent-review"
    _write_json(report_path, report)


def _duplicate_report_file(root: Path) -> None:
    manifest = _manifest(root)
    manifest["reports"]["qa"] = manifest["reports"]["review"]
    _write_json(root / "manifest.json", manifest)


def _unmet_unacknowledged(root: Path) -> None:
    manifest = _manifest(root)
    manifest["unmet_requirements"] = [
        {
            "id": "UNMET-1",
            "description": "Live API acceptance remains with the managing crewmate.",
        }
    ]
    _write_json(root / "manifest.json", manifest)


def _remove_unmet_field(root: Path) -> None:
    manifest = _manifest(root)
    manifest.pop("unmet_requirements")
    _write_json(root / "manifest.json", manifest)


def _unmet_acknowledged(root: Path) -> None:
    _unmet_unacknowledged(root)
    handoff_path = root / "handoff.md"
    text = handoff_path.read_text(encoding="utf-8")
    text = text.replace(
        "None. No live API calls performed; managing crewmate acceptance pending.",
        "UNMET-1: live API acceptance remains with the managing crewmate. "
        "No live API calls performed; managing crewmate acceptance pending.",
    )
    handoff_path.write_text(text, encoding="utf-8")


VERIFIER_MUTATIONS = [
    pytest.param(_drop_gate, "AC-17: no gate record", id="missing-gate"),
    pytest.param(_fail_gate, "AC-17: gate recorded as failed", id="failed-gate"),
    pytest.param(_skip_gate, "AC-17: gate was skipped", id="skipped-gate"),
    pytest.param(_delete_log, "AC-17 log: file is missing", id="missing-log"),
    pytest.param(_empty_log, "AC-17: log is empty", id="empty-log"),
    pytest.param(_add_unknown_gate, "AC-99: gate is not listed", id="unknown-gate"),
    pytest.param(_change_commit, "does not match", id="commit-mismatch"),
    pytest.param(_change_initial_commit, "initial commit", id="initial-commit-rewritten"),
    pytest.param(_change_tree, "does not match", id="tree-mismatch"),
    pytest.param(
        _delete_gate_source_identity,
        "tested_commit must be",
        id="gate-source-identity-deleted",
    ),
    pytest.param(
        _null_gate_source_identity,
        "tested_commit must be",
        id="gate-source-identity-null",
    ),
    pytest.param(
        _wrong_gate_source_commit,
        "does not match",
        id="gate-source-commit-mismatch",
    ),
    pytest.param(
        _wrong_gate_source_tree,
        "does not match",
        id="gate-source-tree-mismatch",
    ),
    pytest.param(
        _wrong_gate_command,
        "does not match the PRD verification command",
        id="wrong-gate-command",
    ),
    pytest.param(
        _weakened_gate_command,
        "does not match the PRD verification command",
        id="weakened-gate-command",
    ),
    pytest.param(
        _extended_gate_command,
        "does not match the PRD verification command",
        id="extended-gate-command",
    ),
    pytest.param(_open_correctness_finding, "unresolved correctness", id="open-correctness"),
    pytest.param(_open_security_finding, "unresolved security", id="open-security"),
    pytest.param(_bare_approval, "bare approval", id="bare-approval"),
    pytest.param(_same_reviewer, "distinct roles", id="same-reviewer"),
    pytest.param(_same_role, "distinct roles", id="same-role"),
    pytest.param(_duplicate_report_file, "distinct files", id="duplicate-report-file"),
    pytest.param(
        _unmet_unacknowledged,
        "not acknowledged",
        id="unmet-unacknowledged",
    ),
    pytest.param(
        _remove_unmet_field,
        "explicit list",
        id="unmet-field-missing",
    ),
]


# --- AC-43 evidence verification -------------------------------------------


def test_complete_fixture_passes_with_explicit_prd(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    result = _run_verifier(root, env=env, prd=root / "prd.md")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    assert "PASS AC-43 self-record" in result.stdout
    assert (root / "ac43_result.json").is_file()


def test_complete_fixture_reads_copied_prd_when_prd_absent(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = _copy_complete(tmp_path)
    result = _run_verifier(root, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


def test_complete_fixture_records_all_45_gate_logs() -> None:
    manifest = _manifest(COMPLETE)
    ids = [gate["id"] for gate in manifest["gates"]]
    assert ids == [f"AC-{number:02d}" for number in range(1, 46)]
    for gate in manifest["gates"]:
        log = COMPLETE / gate["log"]
        assert log.is_file(), gate["id"]
        assert log.read_text(encoding="utf-8").strip(), gate["id"]


def test_missing_prd_fails(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    (root / "prd.md").unlink()
    result = _run_verifier(root, env=env)
    assert result.returncode != 0
    assert "PRD" in result.stdout


def test_prd_gate_table_is_consulted(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    prd_path = root / "prd.md"
    prd_path.write_text(
        re.sub(
            r"^\|\s*AC-17\s*\|.*\n", "", prd_path.read_text(encoding="utf-8"), flags=re.MULTILINE
        ),
        encoding="utf-8",
    )
    result = _run_verifier(root, env=env)
    assert result.returncode != 0
    assert "AC-17" in result.stdout


def test_documented_command_substitutions_are_accepted(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    manifest = _manifest(root)
    for gate in manifest["gates"]:
        command = gate["command"].replace("python ", f"{sys.executable} ")
        command = command.replace("${ARTIFACTS}", str(root)).replace("$ARTIFACTS", str(root))
        gate["command"] = command
    _write_json(root / "manifest.json", manifest)
    result = _run_verifier(root, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


@pytest.mark.parametrize(("mutate", "needle"), VERIFIER_MUTATIONS)
def test_verifier_rejects_mutated_evidence(
    env: dict[str, str], tmp_path: Path, mutate: Callable[[Path], None], needle: str
) -> None:
    root = _copy_complete(tmp_path)
    mutate(root)
    result = _run_verifier(root, env=env)
    assert result.returncode != 0, result.stdout
    assert "RESULT: FAIL" in result.stdout
    assert needle.lower() in result.stdout.lower(), result.stdout
    assert not (root / "ac43_result.json").exists()


def test_explicit_unmet_requirement_is_allowed_when_acknowledged(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = _copy_complete(tmp_path)
    _unmet_acknowledged(root)
    result = _run_verifier(root, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


def test_failing_fixture_is_rejected(env: dict[str, str]) -> None:
    result = _run_verifier(FAILING, env=env)
    assert result.returncode != 0
    assert "AC-07: gate recorded as failed" in result.stdout
    assert "AC-19: gate was skipped" in result.stdout
    assert "unresolved security" in result.stdout


# --- AC-43 self-record bootstrap and read-only confirmation ----------------


def test_bootstrap_then_confirm_is_read_only(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    assert not (root / "ac43_result.json").exists()
    bootstrap = _run_verifier(root, env=env)
    assert bootstrap.returncode == 0, bootstrap.stdout + bootstrap.stderr
    assert "PASS AC-43 self-record" in bootstrap.stdout
    stored = _read_json(root / "ac43_result.json")
    assert stored["status"] == "passed"
    assert stored["gate_count"] == 45

    before = _snapshot(root)
    confirmation = _run_verifier(root, env=env, confirm=True)
    assert confirmation.returncode == 0, confirmation.stdout + confirmation.stderr
    assert "PASS AC-43 confirmation" in confirmation.stdout
    assert _snapshot(root) == before


def test_confirm_without_stored_result_is_read_only(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    before = _snapshot(root)
    result = _run_verifier(root, env=env, confirm=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS AC-43 confirmation" in result.stdout
    assert not (root / "ac43_result.json").exists()
    assert _snapshot(root) == before


def test_failed_run_removes_stale_self_record(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    assert _run_verifier(root, env=env).returncode == 0
    assert (root / "ac43_result.json").exists()
    _drop_gate(root)
    result = _run_verifier(root, env=env)
    assert result.returncode != 0
    assert not (root / "ac43_result.json").exists()


def test_confirm_rejects_tampered_result(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    assert _run_verifier(root, env=env).returncode == 0
    stored = _read_json(root / "ac43_result.json")
    stored["tested_commit"] = "f" * 40
    _write_json(root / "ac43_result.json", stored)
    result = _run_verifier(root, env=env, confirm=True)
    assert result.returncode != 0
    assert "does not match" in result.stdout


# --- AC-45 delivery contract ------------------------------------------------


def test_recorded_delivery_contract_is_clean() -> None:
    root = _evidence_dir()
    assert root.is_dir(), f"evidence directory is missing: {root}"
    assert _audit_delivery(root) == []


def test_complete_fixture_satisfies_delivery_contract() -> None:
    assert _audit_delivery(COMPLETE) == []


def test_delivery_evidence_comes_only_from_env_or_bundled_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(EVIDENCE_ENV, raising=False)
    assert _evidence_dir() == COMPLETE
    monkeypatch.setenv(EVIDENCE_ENV, str(FAILING))
    assert _evidence_dir() == FAILING


@pytest.mark.parametrize(("mutate", "needle"), DELIVERY_MUTATIONS)
def test_delivery_audit_and_verifier_reject_unsafe_record(
    env: dict[str, str],
    tmp_path: Path,
    mutate: Callable[[Path], None],
    needle: str,
) -> None:
    root = _copy_complete(tmp_path)
    mutate(root)
    issues = _audit_delivery(root)
    assert issues, "delivery audit unexpectedly passed"
    assert any(needle.lower() in issue.lower() for issue in issues), issues
    result = _run_verifier(root, env=env)
    assert result.returncode != 0, result.stdout
    assert "RESULT: FAIL" in result.stdout


@pytest.mark.parametrize(("command", "branch", "unsafe", "needle"), GIT_TARGET_CASES)
def test_git_mutation_targets_agree_across_auditors(
    command: str, branch: str | None, unsafe: bool, needle: str
) -> None:
    delivery = _command_issues(command, branch, set(PROTECTED_REFS))
    verifier = _verifier_issues(command, branch)
    assert bool(delivery) is unsafe, delivery
    assert bool(verifier) is unsafe, verifier
    if unsafe:
        assert any(needle.lower() in issue.lower() for issue in delivery), delivery
        assert any(needle.lower() in issue.lower() for issue in verifier), verifier


@pytest.mark.parametrize(("command", "branch", "unsafe", "needle"), SHELL_COMPOSITION_CASES)
def test_shell_composition_audit_agrees_across_auditors(
    command: str, branch: str | None, unsafe: bool, needle: str
) -> None:
    delivery = _command_issues(command, branch, set(PROTECTED_REFS))
    verifier = _verifier_issues(command, branch)
    assert bool(delivery) is unsafe, delivery
    assert bool(verifier) is unsafe, verifier
    if unsafe:
        assert any(needle.lower() in issue.lower() for issue in delivery), delivery
        assert any(needle.lower() in issue.lower() for issue in verifier), verifier


@pytest.mark.parametrize(("mutate", "needle"), EXPLICIT_TARGET_MUTATIONS)
def test_full_verifier_rejects_explicit_or_composed_target_evidence(
    env: dict[str, str],
    tmp_path: Path,
    mutate: Callable[[Path], None],
    needle: str,
) -> None:
    root = _copy_complete(tmp_path)
    mutate(root)
    issues = _audit_delivery(root)
    assert any(needle.lower() in issue.lower() for issue in issues), issues
    result = _run_verifier(root, env=env)
    assert result.returncode != 0, result.stdout
    assert "RESULT: FAIL" in result.stdout
    assert not (root / "ac43_result.json").exists()


def test_confirm_rejects_invalid_target_evidence_without_rewriting(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = _copy_complete(tmp_path)
    _mutate_fetch_destination(root)
    before = _snapshot(root)
    result = _run_verifier(root, env=env, confirm=True)
    assert result.returncode != 0
    assert "RESULT: FAIL" in result.stdout
    assert _snapshot(root) == before


# --- explicit evidence directory and honest audit scope --------------------


def test_verifier_uses_evidence_dir_environment(env: dict[str, str], tmp_path: Path) -> None:
    root = _copy_complete(tmp_path)
    isolated = dict(env, DEEPSEARCH_AXI_EVIDENCE_DIR=str(root))
    result = _run_verifier(None, env=isolated)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


def test_verifier_never_scans_an_implicit_operator_directory(env: dict[str, str]) -> None:
    result = _run_verifier(None, env=env)
    assert result.returncode == 2
    assert EVIDENCE_ENV in result.stderr


def test_output_never_claims_local_git_proves_absence_of_remote_actions(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = _copy_complete(tmp_path)
    result = _run_verifier(root, env=env)
    assert result.returncode == 0
    output = (result.stdout + result.stderr).lower()
    assert REMOTE_SCOPE in output
    assert "proves no remote" not in output
    assert "proves absence of all remote" not in output
