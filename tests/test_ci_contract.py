"""AC-37: CI workflow matrix, required gates, offline guards and no publishing."""

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
    assert "${{ github.workspace }}/tests/fixtures/evidence" in TEXT
