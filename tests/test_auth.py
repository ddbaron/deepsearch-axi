"""Credential precedence, literal dotenv grammar, and safe probe tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from support.env import FAKE_KEY

from deepsearch_axi import models
from deepsearch_axi.auth import check_key, probe_credential, read_dotenv_key, resolve_credential
from deepsearch_axi.guidance import HINT_SET_KEY
from deepsearch_axi.models import CredentialStatus, DeepsearchError

ENV_KEY = "sk-test-ENV-1111111111111111"
CWD_KEY = "sk-test-CWD-2222222222222222"
HOME_KEY = "sk-test-HOME-3333333333333333"
STATES = ("absent", "blank", "present")
HOME_DOTENV = Path(".config") / "deepsearch-axi" / ".env"


def _write_dotenv(path: Path, value: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if value is None:
        path.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    else:
        path.write_text(f"DEEPSEEK_API_KEY={value}\n", encoding="utf-8")


def _instrument_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every ``Path.read_bytes`` attempt made by the resolver."""
    real_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def recording_read_bytes(self: Path) -> bytes:
        reads.append(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    return reads


@pytest.mark.parametrize("env_state", STATES)
@pytest.mark.parametrize("cwd_state", STATES)
@pytest.mark.parametrize("home_state", STATES)
def test_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env_state: str,
    cwd_state: str,
    home_state: str,
) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    env: dict[str, str] = {}
    if env_state == "present":
        env["DEEPSEEK_API_KEY"] = ENV_KEY
    elif env_state == "blank":
        env["DEEPSEEK_API_KEY"] = "   "
    if cwd_state != "absent":
        _write_dotenv(cwd / ".env", CWD_KEY if cwd_state == "present" else None)
    if home_state != "absent":
        _write_dotenv(home / HOME_DOTENV, HOME_KEY if home_state == "present" else None)
    reads = _instrument_reads(monkeypatch)

    cwd_dotenv = cwd / ".env"
    home_dotenv = home / HOME_DOTENV
    if env_state == "present":
        expected = ("env", ENV_KEY)
        expected_reads: list[Path] = []
    elif cwd_state == "present":
        expected = ("cwd_dotenv", CWD_KEY)
        expected_reads = [cwd_dotenv]
    elif home_state == "present":
        expected = ("user_dotenv", HOME_KEY)
        expected_reads = [cwd_dotenv, home_dotenv]
    else:
        expected = None
        expected_reads = [cwd_dotenv, home_dotenv]

    if expected is None:
        with pytest.raises(DeepsearchError) as info:
            resolve_credential(env, cwd, home)
        failure = info.value.failure
        assert failure.code == "missing_key"
        assert models.exit_code_for(failure) == models.EXIT_FAILURE
        for key in (ENV_KEY, CWD_KEY, HOME_KEY):
            assert key not in failure.message
            assert key not in "".join(failure.help)
    else:
        credential = resolve_credential(env, cwd, home)
        assert credential.source == expected[0]
        assert credential.value == expected[1]
        assert expected[1] not in repr(credential)
        assert expected[1] not in str(credential)

    # A usable higher-priority source prevents every lower-priority open.
    assert reads == expected_reads
    captured = capsys.readouterr()
    for key in (ENV_KEY, CWD_KEY, HOME_KEY):
        assert key not in captured.out + captured.err


def test_precedence_ignores_parent_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _write_dotenv(parent / ".env", FAKE_KEY)
    reads = _instrument_reads(monkeypatch)

    with pytest.raises(DeepsearchError) as info:
        resolve_credential({}, child, home)
    assert info.value.failure.code == "missing_key"
    assert parent / ".env" not in reads
    assert reads == [child / ".env", home / HOME_DOTENV]


def test_env_key_is_stripped_and_prevents_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    _write_dotenv(cwd / ".env", CWD_KEY)
    reads = _instrument_reads(monkeypatch)
    credential = resolve_credential({"DEEPSEEK_API_KEY": f"  {ENV_KEY}  "}, cwd, home)
    assert credential == models.Credential(value=ENV_KEY, source="env")
    assert reads == []


_DOTENV_CASES = [
    pytest.param(b"DEEPSEEK_API_KEY=abc123\n", "abc123", None, id="plain"),
    pytest.param(b"DEEPSEEK_API_KEY=abc123", "abc123", None, id="no-trailing-newline"),
    pytest.param(b"export DEEPSEEK_API_KEY=abc123\n", "abc123", None, id="export"),
    pytest.param(b"export  DEEPSEEK_API_KEY=abc123\n", "abc123", None, id="export-extra-space"),
    pytest.param(b"  DEEPSEEK_API_KEY = abc123  \n", "abc123", None, id="assignment-whitespace"),
    pytest.param(b"\tDEEPSEEK_API_KEY\t=\tabc123\t\n", "abc123", None, id="tabs"),
    pytest.param(b'DEEPSEEK_API_KEY="abc123"\n', "abc123", None, id="double-quoted"),
    pytest.param(b"DEEPSEEK_API_KEY='abc123'\n", "abc123", None, id="single-quoted"),
    pytest.param(b'DEEPSEEK_API_KEY="a#b"\n', "a#b", None, id="quoted-hash"),
    pytest.param(b"DEEPSEEK_API_KEY=abc123 # note\n", "abc123", None, id="unquoted-comment"),
    pytest.param(b'DEEPSEEK_API_KEY="abc123" # note\n', "abc123", None, id="quoted-comment"),
    pytest.param(b'DEEPSEEK_API_KEY="abc123"#note\n', "abc123", None, id="quoted-comment-tight"),
    pytest.param(b"DEEPSEEK_API_KEY=abc=123==\n", "abc=123==", None, id="first-equals"),
    pytest.param(b'DEEPSEEK_API_KEY="a=b"\n', "a=b", None, id="quoted-equals"),
    pytest.param(
        b"# heading\nOTHER=one\nDEEPSEEK_API_KEY=abc123\n", "abc123", None, id="unrelated"
    ),
    pytest.param(
        "# caf\u00e9 \U0001f40d\nDEEPSEEK_API_KEY=abc123\n".encode(),
        "abc123",
        None,
        id="utf8-comment",
    ),
    pytest.param(b"OTHER=x\r\nDEEPSEEK_API_KEY=abc123\r\n", "abc123", None, id="crlf"),
    pytest.param(b"DEEPSEEK_API_KEY=abc#def\n", "abc#def", None, id="hash-without-space"),
    pytest.param(b"DEEPSEEK_API_KEY=$(echo-hi)\n", "$(echo-hi)", None, id="command-substitution"),
    pytest.param(b"DEEPSEEK_API_KEY=`id`\n", "`id`", None, id="backticks"),
    pytest.param(b"DEEPSEEK_API_KEY=${HOME}\n", "${HOME}", None, id="variable-reference"),
    pytest.param(b"DEEPSEEK_API_KEY='a\"b'\n", 'a"b', None, id="single-inner-double"),
    pytest.param(b'DEEPSEEK_API_KEY="a\'b"\n', "a'b", None, id="double-inner-single"),
    pytest.param(b"\xef\xbb\xbfDEEPSEEK_API_KEY=abc123\n", "abc123", None, id="bom"),
    pytest.param(b"DEEPSEEK_API_KEY=\n", None, None, id="blank-falls-through"),
    pytest.param(b'DEEPSEEK_API_KEY=""\n', None, None, id="empty-quotes-fall-through"),
    pytest.param(b"DEEPSEEK_API_KEY=   \n", None, None, id="spaces-fall-through"),
    pytest.param(
        b"DEEPSEEK_API_KEY=one\nDEEPSEEK_API_KEY=two\n", None, "duplicate", id="duplicate"
    ),
    pytest.param(
        b"DEEPSEEK_API_KEY=\nDEEPSEEK_API_KEY=two\n",
        None,
        "duplicate",
        id="duplicate-after-blank",
    ),
    pytest.param(b'DEEPSEEK_API_KEY="abc\n', None, "unmatched_quote", id="unmatched-double"),
    pytest.param(b"DEEPSEEK_API_KEY='abc\n", None, "unmatched_quote", id="unmatched-single"),
    pytest.param(b"DEEPSEEK_API_KEY\n", None, "malformed", id="no-equals"),
    pytest.param(b"export DEEPSEEK_API_KEY\n", None, "malformed", id="export-no-equals"),
    pytest.param(b'DEEPSEEK_API_KEY="abc" junk\n', None, "malformed", id="junk-after-quote"),
    pytest.param(b'DEEPSEEK_API_KEY=""x\n', None, "malformed", id="junk-after-empty-quote"),
    pytest.param(b"DEEPSEEK_API_KEY=abc\x01def\n", None, "control_characters", id="control"),
    pytest.param(b"DEEPSEEK_API_KEY=abc def\n", None, "control_characters", id="space-in-key"),
    pytest.param(b"DEEPSEEK_API_KEY=abc\tdef\n", None, "control_characters", id="tab-in-key"),
    pytest.param(
        "DEEPSEEK_API_KEY=caf\u00e9\n".encode(), None, "control_characters", id="non-ascii"
    ),
    pytest.param(b"DEEPSEEK_API_KEY=abc\x7fdef\n", None, "control_characters", id="del"),
    pytest.param(b"DEEPSEEK_API_KEY=abc\xc2\x80def\n", None, "control_characters", id="c1-control"),
]


@pytest.mark.parametrize(("content", "expected", "reason"), _DOTENV_CASES)
def test_literal_dotenv(
    tmp_path: Path, content: bytes, expected: str | None, reason: str | None
) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    (cwd / ".env").write_bytes(content)

    if reason is not None:
        with pytest.raises(DeepsearchError) as info:
            resolve_credential({}, cwd, home)
        failure = info.value.failure
        assert failure.code == "invalid_config"
        assert models.exit_code_for(failure) == models.EXIT_FAILURE
        assert reason.replace("_", " ") in failure.message
        assert "DEEPSEEK_API_KEY" in failure.message
    elif expected is None:
        with pytest.raises(DeepsearchError) as info:
            resolve_credential({}, cwd, home)
        assert info.value.failure.code == "missing_key"
    else:
        credential = resolve_credential({}, cwd, home)
        assert credential.value == expected
        assert credential.source == "cwd_dotenv"


def test_dotenv_blank_falls_through_to_home(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    (cwd / ".env").write_text('DEEPSEEK_API_KEY=""\n', encoding="utf-8")
    _write_dotenv(home / HOME_DOTENV, HOME_KEY)
    credential = resolve_credential({}, cwd, home)
    assert credential.value == HOME_KEY
    assert credential.source == "user_dotenv"


def test_dotenv_unreadable_file_fails_safely(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    (cwd / ".env").mkdir()
    with pytest.raises(DeepsearchError) as info:
        read_dotenv_key(cwd / ".env", "cwd_dotenv")
    failure = info.value.failure
    assert failure.code == "invalid_config"
    assert "unreadable" in failure.message
    assert getattr(info.value, "source", None) == "cwd_dotenv"


def test_dotenv_invalid_encoding_fails_safely(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"DEEPSEEK_API_KEY=\xff\xfe\n")
    with pytest.raises(DeepsearchError) as info:
        read_dotenv_key(path, "user_dotenv")
    assert info.value.failure.code == "invalid_config"
    assert "encoding" in info.value.failure.message


def test_dotenv_size_boundaries(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    prefix = b"DEEPSEEK_API_KEY=abc123\n"
    exact = prefix + b"#" + b"x" * (models.MAX_DOTENV_BYTES - len(prefix) - 2) + b"\n"
    assert len(exact) == models.MAX_DOTENV_BYTES
    (cwd / ".env").write_bytes(exact)
    assert resolve_credential({}, cwd, home).value == "abc123"

    (cwd / ".env").write_bytes(exact + b"x")
    with pytest.raises(DeepsearchError) as info:
        resolve_credential({}, cwd, home)
    assert info.value.failure.code == "invalid_config"
    assert "too large" in info.value.failure.message


def test_dotenv_is_literal_and_never_executes(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    marker = tmp_path / "marker"
    (cwd / ".env").write_text(
        f"DEEPSEEK_API_KEY=$(touch {marker})\nOTHER=`touch {marker}`\n", encoding="utf-8"
    )
    with pytest.raises(DeepsearchError) as info:
        resolve_credential({}, cwd, home)
    assert info.value.failure.code == "invalid_config"
    assert not marker.exists()

    (cwd / ".env").write_text("DEEPSEEK_API_KEY=$(echo-hi)`id`${HOME}\n", encoding="utf-8")
    credential = resolve_credential({}, cwd, home)
    assert credential.value == "$(echo-hi)`id`${HOME}"
    assert not marker.exists()


def test_dotenv_does_not_mutate_process_environment(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    (cwd / ".env").write_text("DEEPSEEK_API_KEY=${HOME}\nOTHER=value\n", encoding="utf-8")
    before = dict(os.environ)
    credential = resolve_credential({}, cwd, home)
    assert credential.value == "${HOME}"
    assert dict(os.environ) == before


def test_check_key_accepts_printable_ascii_and_strips(tmp_path: Path) -> None:
    assert check_key("  abc123  ", "env") == "abc123"
    assert check_key("!~", "env") == "!~"
    assert check_key("A" * 512, "cwd_dotenv") == "A" * 512
    for value in ("a b", "a\tb", "a\nb", "caf\u00e9", "\x7f", "abc\x01"):
        with pytest.raises(DeepsearchError) as info:
            check_key(value, "user_dotenv")
        failure = info.value.failure
        assert failure.code == "invalid_config"
        assert "control characters" in failure.message
        assert getattr(info.value, "source", None) == "user_dotenv"


def test_probe_credential_reports_state_and_source_only(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    assert probe_credential({}, cwd, home) == CredentialStatus("missing", "none")
    configured = probe_credential({"DEEPSEEK_API_KEY": ENV_KEY}, cwd, home)
    assert configured == CredentialStatus("configured", "env")
    assert ENV_KEY not in repr(configured)

    invalid = probe_credential({"DEEPSEEK_API_KEY": "bad\x01"}, cwd, home)
    assert invalid == CredentialStatus("invalid", "env")

    (cwd / ".env").write_text('DEEPSEEK_API_KEY="oops\n', encoding="utf-8")
    assert probe_credential({}, cwd, home) == CredentialStatus("invalid", "cwd_dotenv")

    (cwd / ".env").write_text(f"DEEPSEEK_API_KEY={CWD_KEY}\n", encoding="utf-8")
    assert probe_credential({}, cwd, home) == CredentialStatus("configured", "cwd_dotenv")

    (cwd / ".env").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    (home / ".config" / "deepsearch-axi").mkdir(parents=True)
    (home / HOME_DOTENV).write_bytes(b"DEEPSEEK_API_KEY=\xff\n")
    assert probe_credential({}, cwd, home) == CredentialStatus("invalid", "user_dotenv")


def test_missing_key_failure_is_actionable_and_secret_free(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    with pytest.raises(DeepsearchError) as info:
        resolve_credential({}, cwd, home)
    failure = info.value.failure
    assert failure.code == "missing_key"
    assert models.exit_code_for(failure) == models.EXIT_FAILURE
    assert failure.help[0] == HINT_SET_KEY
    assert "./.env" in failure.message
    assert "~/.config/deepsearch-axi/.env" in failure.message
    assert FAKE_KEY not in failure.message
