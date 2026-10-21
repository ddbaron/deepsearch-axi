#!/usr/bin/env python3
"""Local/offline verifier for the deepsearch-axi external evidence record.

The verifier enforces AC-43 and the audited delivery assertions of AC-45. It
never awards a product gate itself: it checks that the supplied manifest holds
command/result/log records for every gate in the PRD, that each recorded
command matches the literal PRD verification command (allowing only documented
``$ARTIFACTS``/path and local Python-interpreter substitutions), that
independent review and deeper-QA reports are distinct and resolved, and that
the recorded refs, action log and handoff agree with each other.

Expected evidence layout (paths are relative to ``--artifacts``)::

    manifest.json           gate records, tested commit/tree, changed files
    prd.md or plan/prd.md   PRD gate table (when --prd is omitted)
    logs/AC-XX.log          non-empty captured output for each passed gate
    reports/review.json     independent review report
    reports/qa.json         deeper-QA report
    reports/synthesis.json  resolved synthesis of both reports
    refs.json               recorded base/default/protected refs
    action_log.jsonl        audited execution log
    handoff.md              delivery handoff

On success the verifier writes ``ac43_result.json``. ``--confirm`` re-runs the
checks read-only and requires the stored result to match the current record.

AC-43's self-record bootstrap is intentional: no prior AC-43 success is
required. Local git refs and the action log are audited process assertions;
they cannot prove the absence of remote actions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ENV_ARTIFACTS = "DEEPSEARCH_AXI_EVIDENCE_DIR"
PROJECT = "deepsearch-axi"
BOOTSTRAP_GATE = "AC-43"
INITIAL_COMMIT = "2e2bd0a86b3d681b256164b165ffddfcd99e4731"
EXPECTED_GATE_IDS = tuple(f"AC-{number:02d}" for number in range(1, 46))
HANDOFF_REQUIRED_PHRASE = "no live api calls performed; managing crewmate acceptance pending"
REMOTE_SCOPE_PHRASE = "cannot prove absence of remote actions"
DEFAULT_BRANCH_NAMES = frozenset({"main", "master", "trunk", "fm/deepsearch-axi-build-r2"})
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PYTHON_PROGRAM = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
# The second PRD cell is the literal verification command. It may be wrapped in
# backticks; the escapes inside it (for example AC-44's %H%n%B) are literal.
GATE_ROW = re.compile(r"^\|\s*(AC-\d{2})\s*\|\s*(`[^`\n]+`|[^|\n]+?)\s*\|", re.MULTILINE)

# Remote writes, PRs, publication/release, containers, global installation,
# protected-ref mutation, default-branch merges and agent co-author trailers are
# outside the worker's authority. The action log is audited for all of them;
# the log cannot prove that no remote action happened off-log.
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
        "-i",
        "--ignore-case",
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
# ``git rebase [--onto <newbase>] <upstream> [<branch>]``: only the second
# positional operand names the branch that is rewritten. The upstream is a
# source ref and never a mutation target.
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
GIT_REBASE_SHORT_VALUE_OPTIONS = frozenset({"x", "s", "X"})
GIT_REBASE_SHORT_FLAGS = frozenset({"i", "v", "q", "m", "n", "p", "f", "r", "S", "k", "h"})
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


class Audit:
    """Ordered PASS/FAIL diagnostics for one verification run."""

    def __init__(self) -> None:
        self.entries: list[tuple[bool, str]] = []

    def ok(self, message: str) -> None:
        self.entries.append((True, message))

    def fail(self, message: str) -> None:
        self.entries.append((False, message))

    @property
    def passed(self) -> bool:
        return all(ok for ok, _ in self.entries)

    def emit(self) -> None:
        for ok, message in self.entries:
            print(("PASS " if ok else "FAIL ") + message)


@dataclass
class Context:
    """Validated manifest fields used by the downstream checks."""

    feature_branch: str
    initial_commit: str
    tested_commit: str
    tested_tree: str
    changed_files: list[str]
    unmet: list[dict[str, Any]]
    gates: dict[str, dict[str, Any]]
    reports: dict[str, str]
    refs_path: str
    action_log_path: str
    handoff_path: str


def _is_hex40(value: Any) -> bool:
    return isinstance(value, str) and HEX40.fullmatch(value) is not None


def _text(value: Any, minimum: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def _inside(root: Path, relative: Any) -> Path | None:
    if not _text(relative):
        return None
    path = Path(relative)
    if path.is_absolute():
        return None
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _read_text(audit: Audit, root: Path, relative: Any, label: str) -> str | None:
    path = _inside(root, relative)
    if path is None:
        audit.fail(f"{label}: path must be relative to the evidence directory")
        return None
    if not path.is_file():
        audit.fail(f"{label}: file is missing ({relative})")
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        audit.fail(f"{label}: unreadable text ({exc.__class__.__name__})")
        return None


def _load_json(audit: Audit, root: Path, relative: Any, label: str) -> Any:
    text = _read_text(audit, root, relative, label)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        audit.fail(f"{label}: invalid JSON (line {exc.lineno})")
        return None


def _resolve_prd(audit: Audit, root: Path, explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            audit.fail(f"PRD: file is missing ({explicit})")
            return None
        return path
    for candidate in (root / "prd.md", root / "plan" / "prd.md"):
        if candidate.is_file():
            return candidate
    audit.fail("PRD: pass --prd or place prd.md (or plan/prd.md) in the evidence directory")
    return None


def _gate_specs_from_prd(audit: Audit, prd_path: Path) -> dict[str, str]:
    try:
        text = prd_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        audit.fail(f"PRD: unreadable ({exc.__class__.__name__})")
        return {}
    specs: dict[str, str] = {}
    for gate_id, raw_command in GATE_ROW.findall(text):
        command = raw_command.strip().strip("`").strip()
        if gate_id in specs:
            audit.fail(f"PRD: duplicate AC gate row {gate_id}")
            continue
        if not command:
            audit.fail(f"PRD: gate {gate_id} has no verification command")
            continue
        specs[gate_id] = command
    found = set(specs)
    missing = sorted(set(EXPECTED_GATE_IDS) - found)
    extra = sorted(found - set(EXPECTED_GATE_IDS))
    if missing:
        audit.fail("PRD: gate table is missing " + ", ".join(missing))
    if extra:
        audit.fail("PRD: gate table has unexpected " + ", ".join(extra))
    if not specs:
        audit.fail("PRD: no AC gate rows found")
    return {gate_id: specs[gate_id] for gate_id in EXPECTED_GATE_IDS if gate_id in specs}


def _command_tokens(command: str, artifact_spellings: tuple[str, ...]) -> tuple[str, ...] | None:
    """Normalize a literal PRD/recorded command for identity comparison.

    Documented substitutions are canonicalized: ``$ARTIFACTS``/``${ARTIFACTS}``
    are equivalent and may be expanded to the concrete ``--artifacts`` path,
    and any local Python interpreter path is equivalent to ``python``. Extra,
    missing or altered tokens still fail the comparison.
    """
    text = command.strip()
    if not text:
        return None
    for concrete in artifact_spellings:
        if concrete:
            text = text.replace(concrete.rstrip("/"), "$ARTIFACTS")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return None
    normalized: list[str] = []
    for token in tokens:
        token = token.replace("${ARTIFACTS}", "$ARTIFACTS")
        program = Path(token).name.lower()
        if program.endswith(".exe"):
            program = program[:-4]
        normalized.append("python" if PYTHON_PROGRAM.fullmatch(program) else token)
    return tuple(normalized)


def _command_matches(expected: str, recorded: str, artifact_spellings: tuple[str, ...]) -> bool:
    expected_tokens = _command_tokens(expected, artifact_spellings)
    recorded_tokens = _command_tokens(recorded, artifact_spellings)
    return expected_tokens is not None and expected_tokens == recorded_tokens


def _check_gate_source(
    audit: Audit,
    gate_id: str,
    gate: dict[str, Any],
    tested_commit: str,
    tested_tree: str,
) -> int:
    """Require every gate record to identify the tested source tree/commit."""
    problems = 0
    for field, expected, kind in (
        ("tested_commit", tested_commit, "commit"),
        ("tested_tree", tested_tree, "tree"),
    ):
        recorded = gate.get(field)
        if not _is_hex40(recorded):
            audit.fail(f"{gate_id}: {field} must be a full 40-character hex {kind}")
            problems += 1
        elif recorded != expected:
            audit.fail(f"{gate_id}: {field} does not match the manifest")
            problems += 1
    return problems


def _check_gate_command(
    audit: Audit,
    gate_id: str,
    gate: dict[str, Any],
    expected_command: str | None,
    artifact_spellings: tuple[str, ...],
) -> int:
    recorded = gate.get("command")
    if not _text(recorded):
        audit.fail(f"{gate_id}: command is missing")
        return 1
    if expected_command is None:
        audit.fail(f"{gate_id}: no PRD verification command to compare")
        return 1
    if not _command_matches(expected_command, recorded, artifact_spellings):
        audit.fail(
            f"{gate_id}: recorded command does not match the PRD verification command "
            f"({expected_command})"
        )
        return 1
    return 0


def _verify_manifest(audit: Audit, root: Path, raw: Any) -> Context | None:
    if not isinstance(raw, dict):
        audit.fail("manifest: root must be a JSON object")
        return None
    if raw.get("schema_version") != SCHEMA_VERSION:
        audit.fail(f"manifest: schema_version must be {SCHEMA_VERSION}")
    if raw.get("project") != PROJECT:
        audit.fail(f"manifest: project must be {PROJECT!r}")

    feature_branch = raw.get("feature_branch")
    initial_commit = raw.get("initial_commit")
    tested_commit = raw.get("tested_commit")
    tested_tree = raw.get("tested_tree")
    for name, value, expected in (
        ("initial_commit", initial_commit, "commit"),
        ("tested_commit", tested_commit, "commit"),
        ("tested_tree", tested_tree, "tree"),
    ):
        if not _is_hex40(value):
            audit.fail(f"manifest: {name} must be a full 40-character hex {expected}")
    if initial_commit != INITIAL_COMMIT:
        audit.fail(
            f"manifest: initial_commit must be the repository initial commit {INITIAL_COMMIT}"
        )
    if not _text(feature_branch):
        audit.fail("manifest: feature_branch must be a non-empty string")

    changed_files = raw.get("changed_files")
    if not isinstance(changed_files, list) or not changed_files:
        audit.fail("manifest: changed_files must be a non-empty list")
        changed_files = []
    elif not all(_text(item) for item in changed_files):
        audit.fail("manifest: changed_files entries must be non-empty strings")

    unmet_raw = raw.get("unmet_requirements")
    unmet: list[dict[str, Any]] = []
    if not isinstance(unmet_raw, list):
        audit.fail("manifest: unmet_requirements must be an explicit list (use [] for none)")
    else:
        for index, item in enumerate(unmet_raw):
            if (
                not isinstance(item, dict)
                or not _text(item.get("id"))
                or not _text(item.get("description"), 8)
            ):
                audit.fail(
                    f"manifest: unmet_requirements[{index}] needs a non-empty id and description"
                )
            else:
                unmet.append(item)

    reports = raw.get("reports")
    report_paths: dict[str, str] = {}
    if not isinstance(reports, dict):
        audit.fail("manifest: reports must name review, qa and synthesis files")
    else:
        for key in ("review", "qa", "synthesis"):
            value = reports.get(key)
            if not _text(value):
                audit.fail(f"manifest: reports.{key} must be a non-empty path")
            else:
                report_paths[key] = value

    refs_path = raw.get("refs")
    action_log_path = raw.get("action_log")
    handoff_path = raw.get("handoff")
    for label, value in (
        ("refs", refs_path),
        ("action_log", action_log_path),
        ("handoff", handoff_path),
    ):
        if not _text(value):
            audit.fail(f"manifest: {label} must be a non-empty path")

    gates = _parse_gates(audit, raw.get("gates"))
    if (
        not _text(feature_branch)
        or not _is_hex40(initial_commit)
        or not _is_hex40(tested_commit)
        or not _is_hex40(tested_tree)
        or not isinstance(reports, dict)
        or not _text(refs_path)
        or not _text(action_log_path)
        or not _text(handoff_path)
        or not gates
    ):
        return None
    return Context(
        feature_branch=feature_branch.strip(),
        initial_commit=initial_commit,
        tested_commit=tested_commit,
        tested_tree=tested_tree,
        changed_files=[str(item).strip() for item in changed_files],
        unmet=unmet,
        gates=gates,
        reports=report_paths,
        refs_path=refs_path,
        action_log_path=action_log_path,
        handoff_path=handoff_path,
    )


def _parse_gates(audit: Audit, raw: Any) -> dict[str, dict[str, Any]]:
    gates: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list) or not raw:
        audit.fail("manifest: gates must be a non-empty list")
        return gates
    for index, gate in enumerate(raw):
        if not isinstance(gate, dict):
            audit.fail(f"manifest: gates[{index}] must be an object")
            continue
        gate_id = gate.get("id")
        if not _text(gate_id):
            audit.fail(f"manifest: gates[{index}] has no gate id")
            continue
        gate_id = gate_id.strip()
        if gate_id in gates:
            audit.fail(f"{gate_id}: duplicate gate record")
            continue
        gates[gate_id] = gate
    return gates


def _verify_gates(
    audit: Audit,
    root: Path,
    gates: dict[str, dict[str, Any]],
    expected: dict[str, str],
    tested_commit: str,
    tested_tree: str,
    artifact_spellings: tuple[str, ...],
) -> None:
    for gate_id in expected:
        gate = gates.get(gate_id)
        if gate is None:
            audit.fail(f"{gate_id}: no gate record in manifest")
            continue
        status = gate.get("status")
        if status == "failed":
            audit.fail(f"{gate_id}: gate recorded as failed (exit code {gate.get('exit_code')!r})")
            continue
        if status == "skipped":
            audit.fail(f"{gate_id}: gate was skipped; no acceptance gate may skip itself")
            continue
        if status == "pending":
            if gate_id != BOOTSTRAP_GATE:
                audit.fail(f"{gate_id}: gate is pending, not passed")
                continue
            problems = _check_gate_source(audit, gate_id, gate, tested_commit, tested_tree)
            problems += _check_gate_command(
                audit, gate_id, gate, expected.get(gate_id), artifact_spellings
            )
            if problems == 0:
                audit.ok(f"{gate_id}: bootstrap record pending; verifier computes its own result")
            continue
        if status != "passed":
            audit.fail(f"{gate_id}: status is {status!r}, expected 'passed'")
            continue

        problems = _check_gate_source(audit, gate_id, gate, tested_commit, tested_tree)
        problems += _check_gate_command(
            audit, gate_id, gate, expected.get(gate_id), artifact_spellings
        )
        summary = gate.get("summary")
        exit_code = gate.get("exit_code")
        if not _text(summary, 8):
            audit.fail(f"{gate_id}: test summary is missing or too short")
            problems += 1
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0:
            audit.fail(f"{gate_id}: exit_code must be 0, got {exit_code!r}")
            problems += 1
        log_text = _read_text(audit, root, gate.get("log"), f"{gate_id} log")
        if log_text is None:
            problems += 1
        elif not log_text.strip():
            audit.fail(f"{gate_id}: log is empty")
            problems += 1
        if problems == 0:
            audit.ok(f"{gate_id}: command, exit 0, non-empty log and test summary recorded")

    for gate_id in sorted(set(gates) - set(expected)):
        audit.fail(f"{gate_id}: gate is not listed in the PRD gate table")


def _verify_cases(audit: Audit, report: dict[str, Any], label: str) -> int:
    cases = report.get("cases", [])
    if not isinstance(cases, list):
        audit.fail(f"{label}: cases must be a list")
        return 0
    valid_cases = 0
    for index, case in enumerate(cases):
        if (
            isinstance(case, dict)
            and _text(case.get("id"))
            and _text(case.get("description"), 8)
            and case.get("result") in {"pass", "passed"}
        ):
            valid_cases += 1
        else:
            audit.fail(f"{label}: case {index} needs id, concrete description and pass result")
    return valid_cases


def _verify_findings(
    audit: Audit,
    report: dict[str, Any],
    label: str,
    findings: dict[str, dict[str, Any]],
) -> None:
    raw = report.get("findings", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        audit.fail(f"{label}: findings must be a list")
        return
    for index, finding in enumerate(raw):
        if not isinstance(finding, dict):
            audit.fail(f"{label}: finding {index} must be an object")
            continue
        finding_id = finding.get("id")
        severity = finding.get("severity")
        status = finding.get("status")
        if not _text(finding_id):
            audit.fail(f"{label}: finding {index} has no id")
            continue
        finding_id = finding_id.strip()
        if finding_id in findings:
            audit.fail(f"{label}: duplicate finding id {finding_id}")
            continue
        if not _text(severity):
            audit.fail(f"{finding_id}: severity is missing")
        if status not in {"resolved", "open"}:
            audit.fail(f"{finding_id}: status must be resolved or open")
        if not _text(finding.get("summary"), 8):
            audit.fail(f"{finding_id}: summary is missing or too short")
        if status == "resolved" and not _text(finding.get("resolution"), 8):
            audit.fail(f"{finding_id}: resolved finding needs a concrete resolution")
        findings[finding_id] = finding


def _verify_reports(
    audit: Audit,
    root: Path,
    reports: dict[str, str],
    tested_commit: str,
    tested_tree: str,
) -> None:
    if set(reports) != {"review", "qa", "synthesis"}:
        audit.fail("reports: review, qa and synthesis files are all required")
        return
    if len(set(reports.values())) != 3:
        audit.fail("reports: review, qa and synthesis must be distinct files")
    loaded: dict[str, dict[str, Any]] = {}
    for key in ("review", "qa", "synthesis"):
        data = _load_json(audit, root, reports[key], f"reports.{key}")
        if data is None:
            continue
        if not isinstance(data, dict):
            audit.fail(f"reports.{key}: root must be a JSON object")
            continue
        loaded[key] = data

    review = loaded.get("review")
    qa = loaded.get("qa")
    synthesis = loaded.get("synthesis")
    if review is None or qa is None or synthesis is None:
        audit.fail("reports: independent review, deeper QA and synthesis are all required")
        return

    if review.get("role") != "independent-review":
        audit.fail("reports.review: role must be 'independent-review'")
    if qa.get("role") != "deeper-qa":
        audit.fail("reports.qa: role must be 'deeper-qa'")
    if synthesis.get("role") != "synthesis":
        audit.fail("reports.synthesis: role must be 'synthesis'")
    for label, report in (("review", review), ("qa", qa)):
        if not _text(report.get("reviewer")):
            audit.fail(f"reports.{label}: reviewer is missing")
        for field, expected in (("reviewed_commit", tested_commit), ("reviewed_tree", tested_tree)):
            if report.get(field) != expected:
                audit.fail(f"reports.{label}: {field} does not match the tested commit/tree")
    if review.get("reviewer") == qa.get("reviewer"):
        audit.fail("reports: independent review and deeper QA must come from distinct roles")
    if review.get("role") == qa.get("role"):
        audit.fail("reports: independent review and deeper QA must have distinct roles")

    findings: dict[str, dict[str, Any]] = {}
    review_cases = _verify_cases(audit, review, "reports.review")
    qa_cases = _verify_cases(audit, qa, "reports.qa")
    _verify_findings(audit, review, "reports.review", findings)
    _verify_findings(audit, qa, "reports.qa", findings)
    review_findings = review.get("findings") or []
    qa_findings = qa.get("findings") or []
    if review_cases == 0 and not review_findings:
        audit.fail(
            "reports.review: lists no concrete cases or findings; bare approval is not evidence"
        )
    if qa_cases == 0 and not qa_findings:
        audit.fail("reports.qa: lists no concrete cases or findings; bare approval is not evidence")

    open_ids = {key for key, finding in findings.items() if finding.get("status") == "open"}
    for finding_id in sorted(open_ids):
        severity = findings[finding_id].get("severity")
        if severity in {"security", "correctness"}:
            audit.fail(f"{finding_id}: unresolved {severity} finding")
        else:
            audit.fail(f"{finding_id}: unresolved finding; synthesis is not resolved")
    resolved_ids = {key for key, finding in findings.items() if finding.get("status") == "resolved"}
    if len(resolved_ids) != len(findings):
        audit.fail("reports: every finding must be resolved")

    if synthesis.get("status") != "resolved":
        audit.fail("reports.synthesis: status must be 'resolved'")
    if (
        synthesis.get("tested_commit") != tested_commit
        or synthesis.get("tested_tree") != tested_tree
    ):
        audit.fail("reports.synthesis: tested commit/tree does not match the manifest")
    if not _text(synthesis.get("summary"), 20):
        audit.fail("reports.synthesis: summary is missing or too short")
    if not _text(synthesis.get("author")):
        audit.fail("reports.synthesis: author is missing")
    synthesis_resolved = synthesis.get("resolved_findings")
    if not isinstance(synthesis_resolved, list) or set(synthesis_resolved) != set(findings):
        audit.fail("reports.synthesis: resolved_findings must cover every review/QA finding")
    synthesis_open = synthesis.get("open_findings")
    if not isinstance(synthesis_open, list) or set(synthesis_open) != open_ids:
        audit.fail("reports.synthesis: open_findings must match unresolved findings")
    if open_ids:
        audit.fail("reports.synthesis: open findings remain; synthesis is not clean")
    if (
        not open_ids
        and set(synthesis_resolved or []) == set(findings)
        and synthesis.get("status") == "resolved"
    ):
        audit.ok(
            f"reports: independent review and deeper QA are distinct; "
            f"{len(findings)} findings resolved in synthesis"
        )


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^[-*]\s*{re.escape(name)}\s*:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _section(text: str, name: str) -> str | None:
    pattern = rf"^#+\s+{re.escape(name)}\s*$(.*?)(?=^#+\s|\Z)"
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return match.group(1) if match else None


def _first_hex(value: str) -> str | None:
    match = re.search(r"[0-9a-f]{7,40}", value, re.IGNORECASE)
    return match.group(0).lower() if match else None


def _verify_refs(
    audit: Audit,
    root: Path,
    context: Context,
) -> set[str]:
    refs = _load_json(audit, root, context.refs_path, "delivery.refs")
    if not isinstance(refs, dict):
        audit.fail("delivery.refs: refs.json must be a JSON object")
        return set()

    initial = refs.get("initial_commit")
    base_ref = refs.get("base_ref")
    base_commit = refs.get("base_ref_commit")
    default_ref = refs.get("default_ref")
    default_commit = refs.get("default_ref_commit")
    protected = refs.get("protected_refs")
    feature_branch = refs.get("feature_branch")
    feature_commit = refs.get("feature_commit")
    feature_tree = refs.get("feature_tree")
    descends = refs.get("descends_from_initial")
    default_merges = refs.get("default_merges")

    if initial != context.initial_commit:
        audit.fail("delivery.refs: initial_commit does not match the manifest")
    if not _text(base_ref):
        audit.fail("delivery.refs: base_ref is missing")
    if base_commit != context.initial_commit:
        audit.fail("delivery.refs: base ref is not at the recorded initial commit")
    if not _text(default_ref):
        audit.fail("delivery.refs: default_ref is missing")
    if default_commit != context.initial_commit:
        audit.fail("delivery.refs: default ref moved from the initial commit")
    if not isinstance(protected, dict) or not protected:
        audit.fail("delivery.refs: protected_refs must record at least the default branch")
        protected = {}
    else:
        for name, commit in sorted(protected.items()):
            if commit != context.initial_commit:
                audit.fail(f"delivery.refs: protected branch {name} was mutated")
        if _text(default_ref) and default_ref not in protected:
            audit.fail("delivery.refs: default ref is not included in protected_refs")
    if feature_branch != context.feature_branch:
        audit.fail("delivery.refs: feature branch does not match the manifest")
    if feature_branch in DEFAULT_BRANCH_NAMES or feature_branch in {
        base_ref,
        default_ref,
    }:
        audit.fail("delivery.refs: feature branch is a default or protected branch")
    if feature_branch in protected:
        audit.fail("delivery.refs: feature branch is recorded as protected")
    if feature_commit != context.tested_commit:
        audit.fail("delivery.refs: feature commit does not match the tested commit")
    if feature_tree != context.tested_tree:
        audit.fail("delivery.refs: feature tree does not match the tested tree")
    if descends is not True:
        audit.fail("delivery.refs: feature branch does not descend from the initial commit")
    if not isinstance(default_merges, list) or default_merges:
        audit.fail("delivery.refs: a default-branch merge is recorded")

    branch_ok = (
        base_commit == context.initial_commit
        and default_commit == context.initial_commit
        and isinstance(protected, dict)
        and bool(protected)
        and all(value == context.initial_commit for value in protected.values())
        and default_ref in protected
        and feature_branch == context.feature_branch
        and feature_commit == context.tested_commit
        and feature_tree == context.tested_tree
        and descends is True
        and isinstance(default_merges, list)
        and not default_merges
        and feature_branch not in DEFAULT_BRANCH_NAMES
    )
    if branch_ok:
        audit.ok(
            "delivery.refs: base/default/protected refs unchanged at initial commit; "
            "feature branch descends from it"
        )
    return {str(name) for name in protected}


def _program_name(token: str) -> str:
    name = Path(token).name.lower()
    return name[:-4] if name.endswith(".exe") else name


class _ShellSyntaxError(ValueError):
    """Raised when a recorded command cannot be audited without ambiguity."""


def _split_shell_segments(command: str) -> list[list[str]]:
    """Split a recorded command into its executable segments, quote-aware.

    ``&&``, ``||``, ``;``, ``|`` and newlines separate segments; operators
    inside single or double quotes and escaped operators stay literal. Dynamic
    or ambiguous syntax rejects the whole command instead of being skipped.
    """

    segments: list[list[str]] = []
    tokens: list[str] = []
    token: list[str] = []
    started = False
    state = "normal"
    index = 0
    length = len(command)

    def finish_token() -> None:
        nonlocal token, started
        if started:
            tokens.append("".join(token))
        token = []
        started = False

    def finish_segment() -> None:
        nonlocal tokens
        finish_token()
        segments.append(tokens)
        tokens = []

    while index < length:
        character = command[index]
        if state == "single":
            if character == "'":
                state = "normal"
            else:
                token.append(character)
            index += 1
            continue
        if state == "double":
            if character == '"':
                state = "normal"
            elif character == "\\" and index + 1 < length:
                escaped = command[index + 1]
                if escaped != "\n":
                    token.append(escaped if escaped in {"\\", '"', "$", "`"} else character)
                index += 2
                continue
            elif character == "`":
                raise _ShellSyntaxError("backtick command substitution is unsupported")
            elif character == "$" and index + 1 < length and command[index + 1] in {"(", "["}:
                raise _ShellSyntaxError("dynamic substitution is unsupported")
            else:
                token.append(character)
            index += 1
            continue
        if character == "\\":
            if index + 1 >= length:
                raise _ShellSyntaxError("trailing escape is unsupported")
            if command[index + 1] != "\n":
                started = True
                token.append(command[index + 1])
            index += 2
            continue
        if character == "'":
            state = "single"
            started = True
            index += 1
            continue
        if character == '"':
            state = "double"
            started = True
            index += 1
            continue
        if character == "`":
            raise _ShellSyntaxError("backtick command substitution is unsupported")
        if character == "$" and index + 1 < length and command[index + 1] in {"(", "["}:
            raise _ShellSyntaxError("dynamic substitution is unsupported")
        if character == "&":
            if index + 1 < length and command[index + 1] == "&":
                finish_segment()
                index += 2
                continue
            raise _ShellSyntaxError("background '&' is unsupported")
        if character == "|":
            finish_segment()
            index += 2 if index + 1 < length and command[index + 1] == "|" else 1
            continue
        if character == ";":
            finish_segment()
            index += 1
            continue
        if character == "\n":
            finish_segment()
            index += 1
            continue
        if character in "<>":
            raise _ShellSyntaxError("redirect or heredoc syntax is unsupported")
        if character in "()":
            raise _ShellSyntaxError("subshell or grouping syntax is unsupported")
        if character.isspace():
            finish_token()
            index += 1
            continue
        started = True
        token.append(character)
        index += 1

    if state != "normal":
        raise _ShellSyntaxError("unterminated quote is unsupported")
    finish_segment()
    if any(not segment for segment in segments):
        raise _ShellSyntaxError("empty shell segment is unsupported")
    return segments


def _strip_command_prefixes(tokens: list[str]) -> tuple[list[str], bool]:
    """Remove env assignments and shells wrappers such as ``sudo``/``env``."""
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


def _split_git_global(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Skip git global options (``-C``, ``-c``, ``--git-dir``...) to the subcommand."""
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


def _normalize_ref(name: Any) -> str:
    """Fold common protected-ref spellings to a comparable branch name."""
    value = str(name).strip().strip("'\"").strip()
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
    names = {_normalize_ref(name) for name in protected}
    return names | {_normalize_ref(name) for name in DEFAULT_BRANCH_NAMES}


def _is_protected_ref(name: Any, protected: set[str]) -> bool:
    if not _text(name):
        return False
    return _normalize_ref(name) in protected


def _git_rebase_targets(rest: list[str]) -> tuple[set[str], list[str]]:
    """Return the explicitly named rebase branch operands, or fail closed.

    ``git rebase <upstream>`` rewrites the current branch; only a second
    positional operand (``git rebase <upstream> <branch>``) names an explicit
    mutation target.
    """
    positionals: list[str] = []
    modes: set[str] = set()
    saw_root = False
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            positionals.extend(rest[index + 1 :])
            break
        if token.startswith("--"):
            name, separator, _ = token.partition("=")
            if name in GIT_REBASE_MODES:
                modes.add(name)
                index += 1
                continue
            if name in GIT_REBASE_VALUE_OPTIONS:
                if separator:
                    index += 1
                elif index + 1 < len(rest):
                    index += 2
                else:
                    return set(), [f"unsupported rebase option {name}: missing value"]
                continue
            if name in GIT_REBASE_FLAGS:
                saw_root = saw_root or name == "--root"
                index += 1
                continue
            return set(), [f"unsupported rebase option {name}"]
        if token.startswith("-") and token != "-":
            short = token[1:]
            offset = 0
            while offset < len(short):
                flag = short[offset]
                if flag in GIT_REBASE_SHORT_VALUE_OPTIONS:
                    if offset + 1 >= len(short):
                        if index + 1 >= len(rest):
                            return set(), [f"unsupported rebase option -{flag}: missing value"]
                        index += 1
                    offset = len(short)
                    break
                if flag not in GIT_REBASE_SHORT_FLAGS:
                    return set(), [f"unsupported rebase option -{flag}"]
                offset += 1
            index += 1
            continue
        positionals.append(token)
        index += 1
    if modes:
        if len(modes) != 1 or positionals:
            return set(), ["unsupported rebase operation"]
        return set(), []
    if saw_root and positionals:
        return set(), ["unsupported rebase operands with --root"]
    if len(positionals) > 2:
        return set(), ["unsupported rebase operands"]
    if len(positionals) == 2:
        return {positionals[1]}, []
    return set(), []


def _git_fetch_operands(
    rest: list[str],
    *,
    subcommand: str,
) -> tuple[list[str], list[str]]:
    """Parse fetch/pull options and return positional operands, or fail closed."""
    positionals: list[str] = []
    value_options = GIT_FETCH_VALUE_OPTIONS
    flags = GIT_FETCH_FLAGS
    if subcommand == "pull":
        flags = flags | frozenset(
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
        value_options = value_options | frozenset(
            {"--strategy", "-s", "--strategy-option", "-X", "--gpg-sign", "-S"}
        )
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            positionals.extend(rest[index + 1 :])
            break
        if token.startswith("--"):
            name, separator, _ = token.partition("=")
            if name in GIT_FETCH_UNSAFE_OPTIONS:
                return [], [f"unsupported fetch option {name}"]
            if name in value_options:
                if separator:
                    index += 1
                elif index + 1 < len(rest):
                    index += 2
                else:
                    return [], [f"unsupported fetch option {name}: missing value"]
                continue
            if name in flags:
                index += 1
                continue
            return [], [f"unsupported fetch option {name}"]
        if token.startswith("-") and token != "-":
            short = token[1:]
            offset = 0
            while offset < len(short):
                flag = short[offset]
                if flag in {"j", "o", "s", "X"}:
                    if offset + 1 >= len(short):
                        if index + 1 >= len(rest):
                            return [], [f"unsupported fetch option -{flag}: missing value"]
                        index += 1
                    offset = len(short)
                    break
                if f"-{flag}" not in flags:
                    return [], [f"unsupported fetch option -{flag}"]
                offset += 1
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals, []


def _fetch_refspec_issues(
    refspec: str,
    branch: str | None,
    protected: set[str],
) -> list[str]:
    """Audit one fetch/pull refspec's destination, failing closed if unclear."""
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
        if not _text(branch):
            return ["fetch destination HEAD has no branch context"]
        if _is_protected_ref(branch, protected):
            return ["prohibited protected-branch mutation"]
        return []
    if _is_protected_ref(destination, protected):
        return ["prohibited protected-branch mutation"]
    return []


def _audit_git_fetch(
    rest: list[str],
    branch: str | None,
    protected: set[str],
    *,
    subcommand: str,
) -> list[str]:
    positionals, issues = _git_fetch_operands(rest, subcommand=subcommand)
    if issues:
        return issues
    refspecs = positionals[1:]
    if subcommand == "pull" and len(positionals) == 1 and ":" in positionals[0]:
        refspecs = positionals
    for refspec in refspecs:
        issues.extend(_fetch_refspec_issues(refspec, branch, protected))
    return issues


def _current_branch_issues(
    subcommand: str,
    rest: list[str],
    branch: str | None,
    protected: set[str],
    raw: str,
) -> list[str]:
    issues: list[str] = []
    if subcommand == "reset" and "--hard" in rest:
        issues.append("prohibited hard reset")
    if not _text(branch):
        issues.append("mutating command has no branch context")
    elif _is_protected_ref(branch, protected):
        issues.append("prohibited mutation on protected branch")
    if (
        subcommand in {"commit", "merge", "am", "cherry-pick", "revert"}
        and "co-authored-by:" in raw.lower()
    ):
        issues.append("prohibited agent co-author trailer")
    return issues


def _audit_git_branch(rest: list[str], protected: set[str]) -> list[str]:
    mutating = any(
        token in GIT_BRANCH_MUTATION_FLAGS or token.startswith("--force") for token in rest
    )
    if not mutating and any(token in GIT_BRANCH_READONLY_FLAGS for token in rest):
        return []
    targets = _positionals(rest)
    if (mutating or targets) and any(_is_protected_ref(target, protected) for target in targets):
        return ["prohibited protected-branch mutation"]
    return []


def _audit_git_checkout(rest: list[str], protected: set[str]) -> list[str]:
    target: str | None = None
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            break
        if token in GIT_CHECKOUT_CREATE_OPTIONS:
            if index + 1 < len(rest):
                target = rest[index + 1]
            break
        if token in GIT_CHECKOUT_VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        target = token
        break
    if _is_protected_ref(target, protected):
        return ["prohibited protected-branch mutation"]
    return []


def _audit_git(args: list[str], branch: str | None, protected: set[str], raw: str) -> list[str]:
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
        return _audit_git_branch(rest, names)
    if subcommand in {"checkout", "switch"}:
        return _audit_git_checkout(rest, names)
    if subcommand in {"update-ref", "symbolic-ref"}:
        if "--stdin" in rest:
            return ["unsupported update-ref stdin input"]
        if any(
            _is_protected_ref(target, names)
            for target in _positionals(rest, GIT_UPDATE_REF_VALUE_OPTIONS)
        ):
            return ["prohibited protected-branch mutation"]
        return []
    if subcommand == "rebase":
        issues = _current_branch_issues(subcommand, rest, branch, names, raw)
        targets, target_issues = _git_rebase_targets(rest)
        issues.extend(target_issues)
        if any(_is_protected_ref(target, names) for target in targets):
            issues.append("prohibited protected-branch mutation")
        return issues
    if subcommand in {"fetch", "pull"}:
        issues = _audit_git_fetch(rest, branch, names, subcommand=subcommand)
        if subcommand == "pull":
            issues.extend(_current_branch_issues(subcommand, rest, branch, names, raw))
        return issues
    if subcommand in GIT_CURRENT_BRANCH_MUTATORS:
        return _current_branch_issues(subcommand, rest, branch, names, raw)
    return []


def _first_positional(tokens: list[str], value_options: frozenset[str] = frozenset()) -> str | None:
    positionals = _positionals(tokens, value_options)
    return positionals[0] if positionals else None


def _audit_gh(args: list[str]) -> list[str]:
    command = _first_positional(args, GH_GLOBAL_VALUE_OPTIONS)
    if command is None:
        return []
    positionals = _positionals(args, GH_GLOBAL_VALUE_OPTIONS)
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


def _pip_installs_globally(args: list[str], elevated: bool) -> bool:
    if _first_positional(args) != "install":
        return False
    if elevated:
        return True
    return any(
        token in {"--user", "--system"} or token.startswith(("--user=", "--system="))
        for token in args
    )


def _audit_python(args: list[str], elevated: bool) -> list[str]:
    module = None
    rest: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-m" and index + 1 < len(args):
            module = args[index + 1]
            rest = args[index + 2 :]
            break
        if token in {"-c", "-"} or not token.startswith("-"):
            break
        index += 1
    if module == "twine":
        if _first_positional(rest) == "upload":
            return ["prohibited package publication"]
    elif module == "pip":
        if _pip_installs_globally(rest, elevated):
            return ["prohibited global installation"]
    elif module == "pipx":
        if _first_positional(rest) == "install":
            return ["prohibited global installation"]
    elif module == "uv":
        return _audit_uv(rest, elevated)
    return []


def _audit_uv(args: list[str], elevated: bool) -> list[str]:
    positionals = _positionals(args)
    if not positionals:
        return []
    action = positionals[0]
    if action == "publish":
        return ["prohibited package publication"]
    if action == "tool" and len(positionals) > 1 and positionals[1] == "install":
        return ["prohibited global installation"]
    if action == "pip" and len(positionals) > 1 and positionals[1] == "install":
        if elevated or any(
            token in {"--system"} or token.startswith("--system=") for token in args
        ):
            return ["prohibited global installation"]
    return []


def _audit_node_package_manager(program: str, args: list[str]) -> list[str]:
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


def _audit_shell_wrapper(
    program: str,
    args: list[str],
    branch: str | None,
    protected: set[str],
    raw: str,
    depth: int,
) -> list[str]:
    """Recursively audit a statically readable ``sh -c`` payload."""
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
        segments = _split_shell_segments(payload)
    except _ShellSyntaxError as exc:
        return [f"shell wrapper payload cannot be audited safely: {exc}"]
    return _audit_shell_segments(segments, branch, protected, raw, depth + 1)


def _audit_shell_segments(
    segments: list[list[str]],
    branch: str | None,
    protected: set[str],
    raw: str,
    depth: int,
) -> list[str]:
    issues: list[str] = []
    for tokens in segments:
        issues.extend(_audit_segment(tokens, branch, protected, raw, depth))
    return issues


def _audit_segment(
    tokens: list[str],
    branch: str | None,
    protected: set[str],
    raw: str,
    depth: int,
) -> list[str]:
    if any(_program_name(token) == "env" for token in tokens) and any(
        token in {"-S", "--split-string"} or token.startswith(("-S", "--split-string="))
        for token in tokens
    ):
        return ["unsupported env split-string wrapper"]
    tokens, elevated = _strip_command_prefixes(tokens)
    if not tokens:
        return ["command has no executable"]
    program = _program_name(tokens[0])
    args = tokens[1:]
    if program == "eval":
        return ["prohibited dynamic evaluation"]
    if program in SHELL_CONTROL_KEYWORDS:
        return ["unsupported shell control syntax"]
    if program in SHELL_PROGRAMS:
        return _audit_shell_wrapper(program, args, branch, protected, raw, depth)
    issues: list[str] = []
    if program == "git":
        issues.extend(_audit_git(args, branch, protected, raw))
    elif PYTHON_PROGRAM.fullmatch(program):
        issues.extend(_audit_python(args, elevated))
    elif program == "gh":
        issues.extend(_audit_gh(args))
    elif program == "hub":
        if _first_positional(args) == "pull-request":
            issues.append("prohibited pull-request action")
    elif program == "twine":
        if _first_positional(args) == "upload":
            issues.append("prohibited package publication")
    elif program in {"pip", "pip3"}:
        if _pip_installs_globally(args, elevated):
            issues.append("prohibited global installation")
    elif program == "pipx":
        if _first_positional(args) == "install":
            issues.append("prohibited global installation")
    elif program == "uv":
        issues.extend(_audit_uv(args, elevated))
    elif program in {"npm", "pnpm", "yarn", "bun"}:
        issues.extend(_audit_node_package_manager(program, args))
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


def _audit_action_command(command: str, branch: str | None, protected: set[str]) -> list[str]:
    """Return prohibition diagnostics for one recorded action-log command."""
    try:
        segments = _split_shell_segments(command)
    except _ShellSyntaxError as exc:
        issues = [f"command cannot be audited safely: {exc}"]
        if INSTRUCTION_FILE_WRITE.search(command):
            issues.append("prohibited instruction-file edit")
        return issues
    issues = _audit_shell_segments(segments, branch, protected, command, 0)
    if INSTRUCTION_FILE_WRITE.search(command):
        issues.append("prohibited instruction-file edit")
    return issues


def _verify_action_log(
    audit: Audit,
    root: Path,
    context: Context,
    protected: set[str],
) -> None:
    text = _read_text(audit, root, context.action_log_path, "delivery.action_log")
    if text is None:
        return
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            audit.fail(f"delivery.action_log: line {line_number} is not valid JSON")
            continue
        if not isinstance(entry, dict):
            audit.fail(f"delivery.action_log: line {line_number} must be an object")
            continue
        entries.append(entry)
    if not entries:
        audit.fail("delivery.action_log: the audited action log is empty")
        return

    sequences: set[int] = set()
    problems = 0
    for entry in entries:
        seq = entry.get("seq")
        command = entry.get("command")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            audit.fail("delivery.action_log: every entry needs a positive integer seq")
            problems += 1
        elif seq in sequences:
            audit.fail(f"delivery.action_log: duplicate seq {seq}")
            problems += 1
        else:
            sequences.add(seq)
        if not _text(entry.get("actor")):
            audit.fail(f"delivery.action_log: entry {seq!r} has no actor")
            problems += 1
        if not _text(command):
            audit.fail(f"delivery.action_log: entry {seq!r} has no command")
            problems += 1
            continue
        branch = entry.get("branch")
        for issue in _audit_action_command(command, branch if _text(branch) else None, protected):
            audit.fail(f"delivery.action_log: {issue} in entry {seq!r}: {command}")
            problems += 1
    if problems == 0:
        audit.ok(f"delivery.action_log: {len(entries)} entries audited; no prohibited action")


def _verify_handoff(
    audit: Audit,
    root: Path,
    context: Context,
) -> None:
    text = _read_text(audit, root, context.handoff_path, "delivery.handoff")
    if text is None:
        return
    problems = 0

    branch_line = _field(text, "Feature branch")
    if not _text(branch_line):
        audit.fail("delivery.handoff: feature branch is missing")
        problems += 1
    elif branch_line.strip("` ").strip() != context.feature_branch:
        audit.fail("delivery.handoff: feature branch does not match the manifest")
        problems += 1

    commit_line = _field(text, "Final commit")
    commit_value = _first_hex(commit_line) if commit_line else None
    if commit_value is None:
        audit.fail("delivery.handoff: full final commit is missing")
        problems += 1
    elif commit_value != context.tested_commit:
        audit.fail("delivery.handoff: final commit does not match the tested commit")
        problems += 1

    base_line = _field(text, "Initial base")
    base_value = _first_hex(base_line) if base_line else None
    if base_value is None:
        audit.fail("delivery.handoff: initial base commit is missing")
        problems += 1
    elif not context.initial_commit.startswith(base_value):
        audit.fail("delivery.handoff: initial base does not match the manifest")
        problems += 1

    changed = _section(text, "Changed tracked files")
    if changed is None:
        audit.fail("delivery.handoff: changed tracked files section is missing")
        problems += 1
    else:
        bullets = re.findall(r"^\s*[-*]\s+(.+)$", changed, re.MULTILINE)
        paths = {
            match.strip("` ").strip()
            for bullet in bullets
            for match in re.findall(r"`([^`]+)`", bullet)
        }
        if not bullets or not paths:
            audit.fail("delivery.handoff: changed tracked files must list concrete paths")
            problems += 1
        for changed_file in context.changed_files:
            if changed_file not in paths:
                audit.fail(f"delivery.handoff: changed file {changed_file!r} is not listed")
                problems += 1

    validations = _section(text, "Validations")
    if validations is None:
        audit.fail("delivery.handoff: validations section is missing")
        problems += 1
    else:
        lines = validations.splitlines()
        for gate_id, gate in sorted(context.gates.items()):
            if gate_id == BOOTSTRAP_GATE:
                continue
            line = next((item for item in lines if gate_id in item), None)
            if line is None:
                audit.fail(f"delivery.handoff: validation result for {gate_id} is missing")
                problems += 1
                continue
            if "pass" not in line.lower():
                audit.fail(f"delivery.handoff: validation for {gate_id} does not record a pass")
                problems += 1
            log_path = gate.get("log")
            if not _text(log_path) or log_path not in line:
                audit.fail(f"delivery.handoff: validation for {gate_id} lacks its evidence path")
                problems += 1

    synthesis = _section(text, "Review and QA synthesis")
    if synthesis is None:
        audit.fail("delivery.handoff: review and QA synthesis section is missing")
        problems += 1
    else:
        for key in ("review", "qa", "synthesis"):
            path = context.reports.get(key)
            if not path or path not in synthesis:
                audit.fail(f"delivery.handoff: synthesis does not reference reports.{key}")
                problems += 1

    unmet = _section(text, "Unmet requirements")
    if unmet is None:
        audit.fail("delivery.handoff: unmet requirements section is missing")
        problems += 1
    elif context.unmet:
        for item in context.unmet:
            if item["id"] not in unmet:
                audit.fail(
                    f"delivery.handoff: unmet requirement {item['id']!r} is not acknowledged"
                )
                problems += 1
    elif not re.search(r"\bnone\b", unmet, re.IGNORECASE):
        audit.fail("delivery.handoff: unmet requirements must be stated explicitly (or 'None')")
        problems += 1

    lowered = text.lower()
    if HANDOFF_REQUIRED_PHRASE not in lowered:
        audit.fail(
            "delivery.handoff: missing required statement "
            "'no live API calls performed; managing crewmate acceptance pending'"
        )
        problems += 1
    if REMOTE_SCOPE_PHRASE not in lowered:
        audit.fail(
            "delivery.handoff: missing audit-scope statement "
            "'cannot prove absence of remote actions'"
        )
        problems += 1

    if problems == 0:
        audit.ok(
            "delivery.handoff: branch, commit, changed files, validations, synthesis "
            "and unmet items recorded"
        )


def _computed_result(context: Context, expected: dict[str, str], gate_count: int) -> dict[str, Any]:
    return {
        "gate": BOOTSTRAP_GATE,
        "status": "passed",
        "tested_commit": context.tested_commit,
        "tested_tree": context.tested_tree,
        "gate_count": gate_count,
        "expected_gates": len(expected),
        "unmet_requirements": len(context.unmet),
        "scope": "local/offline audited evidence and delivery records",
    }


def _write_result(root: Path, result: dict[str, Any]) -> None:
    path = root / "ac43_result.json"
    temporary = root / ".ac43_result.json.tmp"
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _verify_confirmation(audit: Audit, root: Path, computed: dict[str, Any]) -> None:
    if not (root / "ac43_result.json").is_file():
        audit.ok(
            "AC-43 confirmation: read-only computation passed; "
            "no stored result to compare (bootstrap does not require one)"
        )
        return
    stored = _load_json(audit, root, "ac43_result.json", "AC-43 confirmation")
    if not isinstance(stored, dict):
        audit.fail("AC-43 confirmation: stored ac43_result.json is not a JSON object")
        return
    if stored != computed:
        audit.fail("AC-43 confirmation: stored result does not match the current verification")
        return
    audit.ok("AC-43 confirmation: stored self-record matches; artifacts left unchanged")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a recorded deepsearch-axi evidence tree offline. "
            "Local refs and the action log are audited process assertions, not proof "
            "about remote actions."
        )
    )
    parser.add_argument(
        "--artifacts",
        help=f"evidence directory (defaults to ${ENV_ARTIFACTS})",
    )
    parser.add_argument(
        "--prd",
        help="PRD whose §13 gate table defines required gates (defaults inside --artifacts)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="read-only confirmation of an existing ac43_result.json",
    )
    args = parser.parse_args(argv)

    artifacts = args.artifacts or os.environ.get(ENV_ARTIFACTS)
    if not artifacts:
        print(
            f"verify_evidence: pass --artifacts or set {ENV_ARTIFACTS}",
            file=sys.stderr,
        )
        return 2
    root = Path(artifacts).expanduser().resolve()
    if not root.is_dir():
        print(f"verify_evidence: evidence directory does not exist: {artifacts}", file=sys.stderr)
        return 2
    artifact_spellings = tuple(
        sorted(
            {
                value.rstrip("/")
                for value in (
                    str(root),
                    os.path.abspath(os.path.expanduser(artifacts)),
                    os.path.realpath(os.path.expanduser(artifacts)),
                )
                if value and value.strip("/")
            },
            key=len,
            reverse=True,
        )
    )
    if not args.confirm:
        # A failed run must never leave an earlier passing self-record behind.
        for stale in ("ac43_result.json", ".ac43_result.json.tmp"):
            (root / stale).unlink(missing_ok=True)

    audit = Audit()
    prd_path = _resolve_prd(audit, root, args.prd)
    if prd_path is None:
        audit.emit()
        print("RESULT: FAIL")
        return 1
    expected = _gate_specs_from_prd(audit, prd_path)
    manifest = _load_json(audit, root, "manifest.json", "manifest")
    context = _verify_manifest(audit, root, manifest)
    if context is not None:
        _verify_gates(
            audit,
            root,
            context.gates,
            expected,
            context.tested_commit,
            context.tested_tree,
            artifact_spellings,
        )
        _verify_reports(
            audit,
            root,
            context.reports,
            context.tested_commit,
            context.tested_tree,
        )
        protected = _verify_refs(audit, root, context)
        _verify_action_log(audit, root, context, protected)
        _verify_handoff(audit, root, context)

        if audit.passed:
            computed = _computed_result(context, expected, len(context.gates))
            if args.confirm:
                _verify_confirmation(audit, root, computed)
            else:
                _write_result(root, computed)
                audit.ok(
                    "AC-43 self-record: computed without a prior successful AC-43 record; "
                    "wrote ac43_result.json"
                )

    audit.emit()
    print(f"RESULT: {'PASS' if audit.passed else 'FAIL'}")
    print(
        "AUDIT SCOPE: local refs and the action log are audited process assertions; "
        "local git alone cannot prove absence of remote actions."
    )
    return 0 if audit.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
