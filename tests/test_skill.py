"""AC-34/AC-35: generated skill freshness, read-only check and shared guidance."""

from __future__ import annotations

import runpy
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from support.env import FAKE_KEY

from deepsearch_axi import guidance

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path("scripts/generate_skill.py")
GUIDANCE = Path("src/deepsearch_axi/guidance.py")
SKILL = Path(".agents/skills/deepsearch-axi/SKILL.md")


def _run(
    root: Path, args: Sequence[str], env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / SCRIPT), *args],
        cwd=str(root),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _tree(tmp_path: Path) -> Path:
    """Copy the generator, shared guidance and committed skill into a temporary repo."""
    root = tmp_path / "repo"
    for relative in (SCRIPT, GUIDANCE, SKILL):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)
    return root


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _mutate_guidance(root: Path) -> None:
    guidance_path = root / GUIDANCE
    source = guidance_path.read_text(encoding="utf-8")
    needle = 'DESCRIPTION = "Agent-native CLI for DeepSeek web search and page fetch'
    assert needle in source
    mutated = source.replace(
        needle,
        'DESCRIPTION = "Mutated description for the skill freshness contract',
        1,
    )
    assert mutated != source
    guidance_path.write_text(mutated, encoding="utf-8")


def _frontmatter(text: str) -> tuple[dict[str, str], int]:
    lines = text.splitlines()
    assert lines[0] == "---"
    closing = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        key, separator, value = line.partition(":")
        assert separator == ":"
        fields[key.strip()] = value.strip()
    return fields, closing


def test_check_accepts_fresh_committed_skill_without_writing(env: dict[str, str]) -> None:
    skill_path = REPO_ROOT / SKILL
    before_bytes = skill_path.read_bytes()
    before_mtime = skill_path.stat().st_mtime_ns
    completed = _run(REPO_ROOT, ["--check"], env)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert skill_path.read_bytes() == before_bytes
    assert skill_path.stat().st_mtime_ns == before_mtime


def test_check_rejects_missing_skill_without_creating_it(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    (root / SKILL).unlink()
    before = _files(root)
    run_env = dict(env)
    run_env.pop("PYTHONDONTWRITEBYTECODE", None)
    completed = _run(root, ["--check"], run_env)
    assert completed.returncode == 1
    assert "missing" in completed.stderr
    assert not (root / SKILL).exists()
    assert _files(root) == before


def test_check_rejects_stale_bytes_without_rewriting(env: dict[str, str], tmp_path: Path) -> None:
    root = _tree(tmp_path)
    skill_path = root / SKILL
    skill_path.write_bytes(skill_path.read_bytes() + b"\n")
    stale = skill_path.read_bytes()
    completed = _run(root, ["--check"], env)
    assert completed.returncode == 1
    assert "stale" in completed.stderr
    assert skill_path.read_bytes() == stale


def test_mutating_guidance_fails_check_and_regeneration_restores_success(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    skill_path = root / SKILL
    fresh = skill_path.read_bytes()
    assert _run(root, ["--check"], env).returncode == 0

    _mutate_guidance(root)
    mutating = _run(root, ["--check"], env)
    assert mutating.returncode == 1
    assert "stale" in mutating.stderr
    assert skill_path.read_bytes() == fresh

    regenerated = _run(root, [], env)
    assert regenerated.returncode == 0, regenerated.stderr
    assert skill_path.read_bytes() != fresh
    assert b"Mutated description for the skill freshness contract" in skill_path.read_bytes()
    assert _run(root, ["--check"], env).returncode == 0

    namespace = runpy.run_path(str(root / GUIDANCE))
    assert skill_path.read_text(encoding="utf-8") == namespace["skill_markdown"]()


def test_committed_skill_matches_shared_guidance_and_trigger_frontmatter() -> None:
    text = (REPO_ROOT / SKILL).read_text(encoding="utf-8")
    assert text == guidance.skill_markdown()
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    fields, closing = _frontmatter(text)
    assert fields["name"] == "deepsearch-axi"
    description = fields["description"]
    assert description.startswith('"') and description.endswith('"')
    unquoted = description[1:-1]
    assert unquoted.startswith("Search the web and fetch pages as Markdown through DeepSeek")
    assert "Use when" in unquoted
    assert text.splitlines()[closing + 2] == "# deepsearch-axi"


def test_skill_examples_are_non_interactive_and_repository_sourced() -> None:
    text = (REPO_ROOT / SKILL).read_text(encoding="utf-8")
    assert (
        "uvx --from git+https://github.com/ddbaron/deepsearch-axi.git "
        'deepsearch-axi search "<query>"' in text
    )
    assert "npx" not in text
    assert "pip install deepsearch-axi" not in text
    assert "--api-key" not in text
    assert "$(" not in text
    assert "read -p" not in text
    examples = [
        line.strip()[2:] for line in text.splitlines() if line.startswith("- `deepsearch-axi ")
    ]
    assert examples
    for example in examples:
        assert example.startswith("`deepsearch-axi ")
        assert example.endswith("`")


def test_skill_contains_no_live_context_or_secrets() -> None:
    text = (REPO_ROOT / SKILL).read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    assert "bin:" not in text
    assert "cwd:" not in text
    assert "manifest" not in text
    assert "credential state" not in text
    assert "/Users/" not in text
    assert str(REPO_ROOT) not in text
    for token in ("DEEPSEEK_BASE_URL", "sk-", "-----BEGIN", "API_KEY="):
        assert token not in text


def test_skill_generator_reads_the_single_guidance_source() -> None:
    source = (REPO_ROOT / SCRIPT).read_text(encoding="utf-8")
    assert "guidance.py" in source
    assert "skill_markdown" in source
    assert "Search the web and fetch pages as Markdown" not in source
    rendered = guidance.skill_markdown()
    assert guidance.DESCRIPTION in rendered
    assert guidance.PROJECT_INSTRUCTION in rendered
    for path, spec in guidance.COMMANDS.items():
        if path in ((), ("session",)):
            continue
        assert spec.usage in rendered


def test_generator_does_not_depend_on_installed_package(
    env: dict[str, str], tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    run_env = dict(env)
    run_env.pop("PYTHONPATH", None)
    run_env.pop("PYTHONDONTWRITEBYTECODE", None)
    completed = _run(root, ["--check"], run_env)
    assert completed.returncode == 0, completed.stderr
    assert not (root / "src" / "deepsearch_axi" / "__pycache__").exists()
