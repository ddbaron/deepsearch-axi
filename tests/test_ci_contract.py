"""AC-37: CI workflow matrix, required gates, offline guards and no publishing."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
EVIDENCE_ENV = "DEEPSEARCH_AXI_EVIDENCE_DIR"
WORKSPACE_PREFIX = "${{ github.workspace }}/"
COMPLETE_EVIDENCE = f"{WORKSPACE_PREFIX}tests/fixtures/evidence/complete"
DELIVERY_TEST = "tests/test_delivery_contract.py::test_recorded_delivery_contract_is_clean"
REQUIRED_COMMANDS = (
    "python -m pytest -q",
    "python -m ruff check .",
    "python -m ruff format --check .",
    "rumdl check --disable MD013 README.md .agents/skills/deepsearch-axi/SKILL.md",
    "python scripts/generate_skill.py --check",
    "python -m build",
    "python -m twine check dist/*",
    "tests/test_packaging.py",
    "tests/test_installation.py",
    "tests/test_opencode.py",
)
JOB_HEADER = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_-]*):[ \t]*$", re.MULTILINE)
FULL_SUITE_STEP = re.compile(r"^\s*run:\s*python -m pytest -q\s*$", re.MULTILINE)


def _job_blocks() -> dict[str, str]:
    """Split the workflow into top-level job blocks without a YAML dependency."""
    marker = "\njobs:\n"
    start = TEXT.index(marker) + len(marker)
    section = TEXT[start:]
    headers = list(JOB_HEADER.finditer(section))
    blocks: dict[str, str] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(section)
        blocks[header.group(1)] = section[header.start() : end]
    return blocks


def _env_values(block: str, name: str) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(name)}:\s*(.+?)\s*$", re.MULTILINE)
    return [value.strip().strip("\"'") for value in pattern.findall(block)]


def _selected_evidence_dir() -> Path:
    """Resolve the workflow's ``${{ github.workspace }}`` to this checkout."""
    assert COMPLETE_EVIDENCE.startswith(WORKSPACE_PREFIX)
    return REPO_ROOT / COMPLETE_EVIDENCE[len(WORKSPACE_PREFIX) :]


def _run_recorded_delivery_test(evidence_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[EVIDENCE_ENV] = str(evidence_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", DELIVERY_TEST],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_workflow_covers_every_supported_python_and_macos() -> None:
    matrix = re.search(r"python-version:\s*\[([^\]]+)\]", TEXT)
    assert matrix is not None
    versions = re.findall(r'"([^"]+)"', matrix.group(1))
    assert versions == ["3.10", "3.11", "3.12", "3.13", "3.14"]
    assert "allow-prereleases: true" in TEXT
    assert "ubuntu-latest" in TEXT
    assert "macos-latest" in TEXT
    assert 'python-version: "3.12"' in TEXT


def test_workflow_runs_every_required_offline_gate() -> None:
    for command in REQUIRED_COMMANDS:
        assert command in TEXT, f"missing CI command: {command}"
    assert "actions/setup-node@v4" in TEXT
    assert 'node-version: "20"' in TEXT
    # The deferred plugin deliverable must stay required rather than being
    # deleted, disabled, or allowed to fail without breaking CI.
    assert "continue-on-error" not in TEXT
    assert "|| true" not in TEXT
    assert "if: false" not in TEXT


def test_workflow_requires_no_secrets_live_calls_or_publishing() -> None:
    assert "secrets." not in TEXT
    assert re.search(r"^permissions:\n  contents: read$", TEXT, re.MULTILINE) is not None
    for forbidden in (
        "twine upload",
        "gh release",
        "git push",
        "docker push",
        "pypa/gh-action-pypi-publish",
        "softprops/action-gh-release",
        "upload-release",
        "pull_request_target",
        "DEEPSEEK_API_KEY:",
        "api.deepseek.com",
    ):
        assert forbidden not in TEXT, f"workflow contains forbidden token: {forbidden}"


def test_workflow_guards_subprocess_networking_for_tests() -> None:
    assert 'DEEPSEARCH_AXI_NETGUARD: "1"' in TEXT
    assert TEXT.count('DEEPSEARCH_AXI_NETGUARD: "1"') >= 2
    env_source = (REPO_ROOT / "tests" / "support" / "env.py").read_text(encoding="utf-8")
    assert '"DEEPSEARCH_AXI_NETGUARD": "1"' in env_source


def test_full_suite_jobs_select_the_complete_evidence_fixture() -> None:
    jobs = _job_blocks()
    full_suite = {name: block for name, block in jobs.items() if FULL_SUITE_STEP.search(block)}
    assert set(full_suite) == {"test", "macos"}
    for name, block in full_suite.items():
        values = _env_values(block, EVIDENCE_ENV)
        assert values == [COMPLETE_EVIDENCE], (
            f"{name} must select exactly {COMPLETE_EVIDENCE}, found {values}"
        )


def test_ci_selected_evidence_directory_contains_the_manifest() -> None:
    fixture = _selected_evidence_dir()
    assert fixture.is_dir(), f"CI selects a missing evidence directory: {fixture}"
    assert (fixture / "manifest.json").is_file()
    assert (fixture / "prd.md").is_file()


def test_recorded_delivery_contract_passes_with_ci_equivalent_fixture() -> None:
    result = _run_recorded_delivery_test(_selected_evidence_dir())
    assert result.returncode == 0, result.stdout + result.stderr


def test_recorded_delivery_contract_fails_closed_for_invalid_explicit_directory(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty-evidence"
    empty.mkdir()
    legacy_parent = REPO_ROOT / "tests" / "fixtures" / "evidence"
    for invalid in (empty, legacy_parent):
        assert not (invalid / "manifest.json").is_file()
        result = _run_recorded_delivery_test(invalid)
        assert result.returncode != 0, (
            f"{invalid} was accepted as evidence instead of failing closed"
        )
