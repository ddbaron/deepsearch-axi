"""Boundary grammar for owned hook matchers used by home readiness.

Readiness never compiles a host-controlled regex. The accepted grammar is the
match-all forms plus Claude Code's exact-match alternation of documented
lifecycle identifiers. Alternatives are literal: whitespace is significant and
never trimmed, and the length cap is inclusive at 256.
"""

from __future__ import annotations

import pytest

from deepsearch_axi.home import _matcher_covers

_LIMIT = 256
_CLAUDE_START = "startup|resume|clear|compact"
_CLAUDE_END = "clear|resume|logout|prompt_input_exit|other"


def _covering_matcher_at_limit() -> str:
    """Return a valid SessionStart alternation of exactly the length cap."""
    matcher = _CLAUDE_START + "|compact" * 27 + "|clear" * 2
    assert len(matcher) == _LIMIT
    return matcher


def test_length_cap_boundary_is_inclusive() -> None:
    at_limit = _covering_matcher_at_limit()
    assert _matcher_covers(at_limit, "SessionStart", "claude-code") is True
    assert _matcher_covers(at_limit + "|clear", "SessionStart", "claude-code") is False


@pytest.mark.parametrize(
    ("matcher", "event", "expected"),
    [
        (None, "SessionStart", False),
        (123, "SessionStart", False),
        (["startup"], "SessionStart", False),
        ("*", "SessionStart", True),
        ("", "SessionEnd", True),
        (_CLAUDE_START, "SessionStart", True),
        (_CLAUDE_END, "SessionEnd", True),
        ("startup", "SessionStart", False),
        ("startup|resume|clear", "SessionStart", False),
        ("startup||resume|clear|compact", "SessionStart", False),
        ("startup|resume|clear|compact|bogus", "SessionStart", False),
        ("^startup|resume|clear|compact$", "SessionStart", False),
        ("^(.?){50}X$", "SessionStart", False),
        ("clear|resume|logout|prompt_input_exit", "SessionEnd", False),
        ("clear|resume|logout|prompt_input_exit|other", "SessionStart", False),
        # Whitespace is literal in the native matcher, never trimmed.
        (" startup | resume | clear | compact ", "SessionStart", False),
        ("startup |resume|clear|compact", "SessionStart", False),
        ("startup| resume |clear|compact", "SessionStart", False),
        ("startup\t|resume|clear|compact", "SessionStart", False),
        ("clear | resume | logout | prompt_input_exit | other", "SessionEnd", False),
    ],
)
def test_matcher_grammar(matcher: object, event: str, expected: bool) -> None:
    assert _matcher_covers(matcher, event, "claude-code") is expected
