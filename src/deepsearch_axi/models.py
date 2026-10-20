from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, Union

JsonValue = Union[None, bool, int, float, str, List["JsonValue"], Dict[str, "JsonValue"]]
JsonObject = Dict[str, JsonValue]

Tool = Literal["search", "fetch"]
CredentialSource = Literal["env", "cwd_dotenv", "user_dotenv"]
AgentName = Literal["claude-code", "codex", "opencode"]
AgentSelection = Literal["claude-code", "codex", "opencode", "all"]
IntegrationState = Literal["missing", "installed", "stale"]
SessionEventKind = Literal["start", "checkpoint", "end"]

AGENTS: Tuple[AgentName, ...] = ("claude-code", "codex", "opencode")
OFFICIAL_ORIGIN = "https://api.deepseek.com"
TOOL_PATHS = {"search": "/v1/tools/search", "fetch": "/v1/tools/fetch"}
TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # read at most this + 1
MAX_NATIVE_PAYLOAD_BYTES = 65536
MAX_STATE_FILE_BYTES = 65536
MAX_SESSION_RECORDS = 20
MAX_SCOPE_RECORDS = 20
MAX_DOTENV_BYTES = 65536

SEARCH_FIELDS: Tuple[str, ...] = (
    "title",
    "url",
    "snippet",
    "summary",
    "content",
    "date",
    "authority",
    "rerank_score",
    "site_name",
    "site_icon",
)
DEFAULT_FIELDS: Tuple[str, ...] = ("title", "url", "snippet")
DEFAULT_FIELDS_WITH_CONTENT: Tuple[str, ...] = ("title", "url", "snippet", "content")
TEXT_LIMITS = {"title": 200, "snippet": 200, "summary": 1000, "content": 1000}
SEARCH_DEFAULT_COUNT = 10
QUERY_MAX = 128
ORI_QUESTION_MAX = 128
FETCH_URL_MAX = 8192
REDACTED = "[REDACTED]"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


@dataclass(frozen=True)
class Credential:
    value: str = field(repr=False)
    source: CredentialSource

    def __str__(self) -> str:
        return f"Credential(source={self.source})"


@dataclass(frozen=True)
class CredentialStatus:
    state: Literal["configured", "missing", "invalid"]
    source: Literal["env", "cwd_dotenv", "user_dotenv", "none"]


@dataclass(frozen=True)
class SearchOptions:
    query: str
    count: int = SEARCH_DEFAULT_COUNT
    need_content: bool = False
    ori_question: Optional[str] = None
    localization: str = ""


@dataclass(frozen=True)
class OutputOptions:
    fields: Tuple[str, ...] = DEFAULT_FIELDS
    full: bool = False


@dataclass(frozen=True)
class HttpReply:
    status: int
    payload: Optional[JsonObject]  # None for every non-200 status (body never read)


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    help: Tuple[str, ...]  # 1 or 2 items
    http_status: Optional[int] = None


class DeepsearchError(Exception):
    """The only exception type allowed to cross component boundaries."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)  # never put user/secret text in args
        self.failure = failure


def exit_code_for(failure: Failure) -> int:
    return EXIT_USAGE if failure.code == "usage" else EXIT_FAILURE


# ---- parsed commands (argv.py output) ----
@dataclass(frozen=True)
class HomeCommand:
    pass


@dataclass(frozen=True)
class HelpCommand:
    # (), ("search",), ("fetch",), ("setup",), ("session",),
    # ("session", "start"), ("session", "end")
    path: Tuple[str, ...]


@dataclass(frozen=True)
class SearchCommand:
    options: SearchOptions
    output: OutputOptions


@dataclass(frozen=True)
class FetchCommand:
    url: str
    full: bool = False


@dataclass(frozen=True)
class SetupCommand:
    agent: AgentSelection = "all"


@dataclass(frozen=True)
class SessionCommand:
    event: Literal["start", "end"]
    agent: AgentName
    project: Path  # absolute as given; resolved later


ParsedCommand = Union[
    HomeCommand, HelpCommand, SearchCommand, FetchCommand, SetupCommand, SessionCommand
]


# ---- local context (home / ambient) ----
@dataclass(frozen=True)
class Activity:
    search_ok: int = 0
    search_fail: int = 0
    fetch_ok: int = 0
    fetch_fail: int = 0


@dataclass(frozen=True)
class PriorEvent:
    agent: str  # AgentName
    event: SessionEventKind
    sessions: int  # retained session records in this scope


@dataclass(frozen=True)
class LocalContext:
    bin: str  # absolute executable path, HOME prefix collapsed to "~"
    cwd: str  # actual cwd, HOME prefix collapsed to "~"
    auth: CredentialStatus
    origin: str  # validated origin or the literal "invalid"
    origin_state: Literal["ready", "invalid"]
    integrations: Tuple[Tuple[AgentName, IntegrationState], ...]  # always AGENTS order
    prior: Optional[PriorEvent] = None
    activity: Optional[Activity] = None


# ---- setup ----
@dataclass(frozen=True)
class ExecutableIdentity:
    path: Path  # absolute, not symlink-resolved, running console script
    command: Tuple[str, ...]  # ("deepsearch-axi",) or (str(path),)


@dataclass(frozen=True)
class FileChange:
    path: Path  # absolute, inside project root
    before: Optional[bytes]  # None = file did not exist
    after: bytes


@dataclass(frozen=True)
class HostOutcome:
    agent: AgentName
    status: Literal["installed", "repaired", "unchanged"]
    files: Tuple[str, ...]  # project-relative POSIX paths owned for this host


@dataclass(frozen=True)
class SetupResult:
    root: Path
    outcomes: Tuple[HostOutcome, ...]  # selection order (AGENTS order for "all")
    changed: bool  # True iff any byte on disk changed


# ---- session ----
@dataclass(frozen=True)
class NativeEvent:
    session_hash: str  # 32 lowercase hex chars
    scope: str  # cwd relative to root, POSIX, "." for root
    kind: SessionEventKind


@dataclass(frozen=True)
class SessionResult:
    document: JsonObject
    recorded: bool  # state file changed
