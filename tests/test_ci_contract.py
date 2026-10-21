"""CI workflow contract: real commands, offline guards and no evidence framework."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
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
WHEELHOUSE_STEP = re.compile(r"python -m pip download .*--dest wheelhouse", re.MULTILINE)


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


def test_full_suite_jobs_provision_the_wheelhouse_before_testing() -> None:
    jobs = _job_blocks()
    full_suite = {name: block for name, block in jobs.items() if FULL_SUITE_STEP.search(block)}
    assert set(full_suite) == {"test", "macos"}
    for name, block in full_suite.items():
        assert WHEELHOUSE_STEP.search(block) is not None, f"{name} does not provision wheels"
        assert block.index("pip install pipx uv") < block.index("python -m pytest -q"), name


def test_workflow_has_no_evidence_framework_references() -> None:
    assert "tests/fixtures/evidence" not in TEXT
    assert "verify_evidence" not in TEXT
    assert "test_delivery_contract" not in TEXT
    assert "DEEPSEARCH_AXI_EVIDENCE" not in TEXT


def test_evidence_framework_is_deleted() -> None:
    assert not (REPO_ROOT / "scripts" / "verify_evidence.py").exists()
    assert not (REPO_ROOT / "tests" / "test_delivery_contract.py").exists()
    assert not (REPO_ROOT / "tests" / "fixtures" / "evidence").exists()


def test_native_setup_and_plugin_tests_are_delivered() -> None:
    assert (REPO_ROOT / "tests" / "test_setup.py").is_file()
    assert (REPO_ROOT / "tests" / "test_opencode.py").is_file()
