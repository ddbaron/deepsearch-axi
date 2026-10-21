"""CI workflow contract: real commands, offline guards and no evidence framework."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from support.env import isolated_env

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REQUIRED_RUNS: dict[str, tuple[str, ...]] = {
    "test": (
        "python -m pytest -q",
        "python -m pytest -q tests/test_setup.py tests/test_opencode.py",
    ),
    "macos": ("python -m pytest -q",),
    "lint": (
        "python -m ruff check .",
        "python -m ruff format --check .",
        "rumdl check --disable MD013 README.md .agents/skills/deepsearch-axi/SKILL.md",
        "python scripts/generate_skill.py --check",
    ),
    "package": (
        "python -m build",
        "python scripts/check_version.py",
        "python -m twine check dist/*",
        "python -m pytest -q tests/test_packaging.py tests/test_installation.py",
    ),
}
FORBIDDEN_WORKFLOW_TEXT = (
    "secrets.",
    "twine upload",
    "gh release",
    "git push",
    "docker push",
    "pypa/gh-action-pypi-publish",
    "softprops/action-gh-release",
    "upload-release",
    "pull_request_target",
    "DEEPSEEK_API_KEY",
    "api.deepseek.com",
)
EVIDENCE_TOKENS = (
    "tests/fixtures/evidence",
    "verify_evidence",
    "test_delivery_contract",
    "DEEPSEARCH_AXI_EVIDENCE",
)


def _document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _jobs() -> dict[str, dict[str, Any]]:
    jobs = _document().get("jobs")
    assert isinstance(jobs, dict)
    assert jobs
    for job in jobs.values():
        assert isinstance(job, dict)
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert steps
    for step in steps:
        assert isinstance(step, dict)
    return steps


def _commands(step: dict[str, Any]) -> tuple[str, ...]:
    run = step.get("run")
    if not isinstance(run, str):
        return ()
    return tuple(line.strip() for line in run.splitlines() if line.strip())


def _job_commands(job: dict[str, Any]) -> tuple[str, ...]:
    return tuple(command for step in _steps(job) for command in _commands(step))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _step_using(job: dict[str, Any], action: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if step.get("uses") == action]
    assert len(matches) == 1, f"expected one {action} step"
    return matches[0]


def _effective_env(document: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    environment = dict(document.get("env") or {})
    environment.update(step.get("env") or {})
    return environment


def test_workflow_covers_every_supported_python_and_macos() -> None:
    jobs = _jobs()
    test = jobs["test"]
    assert test["runs-on"] == "ubuntu-latest"
    matrix = test["strategy"]["matrix"]
    assert matrix["python-version"] == ["3.10", "3.11", "3.12", "3.13", "3.14"]
    setup_python = _step_using(test, "actions/setup-python@v5")
    assert setup_python["with"]["allow-prereleases"] is True

    macos = jobs["macos"]
    assert macos["runs-on"] == "macos-latest"
    assert _step_using(macos, "actions/setup-python@v5")["with"]["python-version"] == "3.12"

    for name in ("test", "macos"):
        node = _step_using(jobs[name], "actions/setup-node@v4")
        assert node["with"]["node-version"] == "20"

    for job in jobs.values():
        assert any(step.get("uses") == "actions/checkout@v4" for step in _steps(job))


def test_workflow_runs_every_required_offline_gate() -> None:
    jobs = _jobs()
    for name, required in REQUIRED_RUNS.items():
        assert name in jobs
        commands = _job_commands(jobs[name])
        for command in required:
            assert commands.count(command) == 1, f"{name} must run exactly once: {command}"
    for name, job in jobs.items():
        assert "if" not in job, f"{name} can be skipped"
        for step in _steps(job):
            assert "if" not in step, f"{name} step can be skipped: {step.get('name')}"
            assert step.get("continue-on-error") in (None, False)
        for command in _job_commands(job):
            assert "|| true" not in command


def test_workflow_requires_no_secrets_live_calls_or_publishing() -> None:
    document = _document()
    assert document["permissions"] == {"contents": "read"}
    texts = tuple(_strings(document))
    for forbidden in FORBIDDEN_WORKFLOW_TEXT:
        assert not any(forbidden in text for text in texts), forbidden


def test_workflow_guards_subprocess_networking_for_tests(tmp_path: Path) -> None:
    document = _document()
    jobs = _jobs()
    for name in ("test", "macos", "package"):
        for step in _steps(jobs[name]):
            if any("pytest" in command for command in _commands(step)):
                environment = _effective_env(document, step)
                assert environment["DEEPSEARCH_AXI_NETGUARD"] == "1", name

    environment = isolated_env(tmp_path / "home")
    assert environment["DEEPSEARCH_AXI_NETGUARD"] == "1"


def test_full_suite_jobs_provision_the_wheelhouse_before_testing() -> None:
    jobs = _jobs()
    full_suite = {
        name: job for name, job in jobs.items() if "python -m pytest -q" in _job_commands(job)
    }
    assert set(full_suite) == {"test", "macos"}
    for name, job in full_suite.items():
        commands_by_step = [_commands(step) for step in _steps(job)]
        install_index = next(
            index
            for index, commands in enumerate(commands_by_step)
            if "python -m pip install pipx uv" in commands
        )
        wheelhouse_index = next(
            index
            for index, commands in enumerate(commands_by_step)
            if any(
                command.startswith("python -m pip download") and "--dest wheelhouse" in command
                for command in commands
            )
        )
        pytest_index = next(
            index
            for index, commands in enumerate(commands_by_step)
            if "python -m pytest -q" in commands
        )
        assert install_index < wheelhouse_index < pytest_index, name


def test_workflow_has_no_evidence_framework_references() -> None:
    texts = tuple(_strings(_document()))
    for token in EVIDENCE_TOKENS:
        assert not any(token in text for text in texts), token


def test_evidence_framework_is_deleted() -> None:
    assert not (REPO_ROOT / "scripts" / "verify_evidence.py").exists()
    assert not (REPO_ROOT / "tests" / "test_delivery_contract.py").exists()
    assert not (REPO_ROOT / "tests" / "fixtures" / "evidence").exists()


def test_native_setup_and_plugin_tests_are_delivered() -> None:
    assert (REPO_ROOT / "tests" / "test_setup.py").is_file()
    assert (REPO_ROOT / "tests" / "test_opencode.py").is_file()
