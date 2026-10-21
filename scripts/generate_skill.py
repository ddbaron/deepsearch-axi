#!/usr/bin/env python3
"""Generate the committed agent skill from the shared static guidance.

``deepsearch_axi/guidance.py`` is the single source for the description, command
schema, hints, project instruction and skill text. This script loads that file
directly by path so it works in a source checkout without installing the
package. ``--check`` is read-only: it exits 0 when the committed ``SKILL.md`` is
byte-fresh and 1 when it is missing or stale.
"""

from __future__ import annotations

import argparse
import runpy
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDANCE_PATH = REPO_ROOT / "src" / "deepsearch_axi" / "guidance.py"
SKILL_PATH = REPO_ROOT / ".agents" / "skills" / "deepsearch-axi" / "SKILL.md"


def load_skill_markdown(guidance_path: Path = GUIDANCE_PATH) -> Callable[[], str]:
    """Load ``skill_markdown`` from the guidance module without importing the package."""
    if not guidance_path.is_file():
        raise FileNotFoundError(f"guidance module not found: {guidance_path}")
    namespace = runpy.run_path(str(guidance_path), run_name="deepsearch_axi_guidance")
    renderer = namespace.get("skill_markdown")
    if not callable(renderer):
        raise AttributeError(f"{guidance_path} does not define skill_markdown()")
    return renderer  # type: ignore[return-value]


def render_skill(guidance_path: Path = GUIDANCE_PATH) -> str:
    """Return the deterministic skill document as a string."""
    return load_skill_markdown(guidance_path)()


def _stale(path: Path, reason: str) -> int:
    try:
        label = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        label = str(path)
    print(
        f"stale: {label} {reason}; run python scripts/generate_skill.py",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or verify the committed deepsearch-axi agent skill.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify SKILL.md is byte-fresh without writing; exit 1 when stale or missing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    expected = render_skill().encode("utf-8")
    if args.check:
        try:
            current = SKILL_PATH.read_bytes()
        except FileNotFoundError:
            return _stale(SKILL_PATH, "is missing")
        except OSError as error:
            print(f"error: cannot read {SKILL_PATH}: {error}", file=sys.stderr)
            return 1
        if current != expected:
            return _stale(SKILL_PATH, "does not match the shared guidance")
        return 0

    SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_PATH.write_bytes(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
