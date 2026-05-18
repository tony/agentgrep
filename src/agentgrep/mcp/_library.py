"""Library facade for the ``agentgrep`` MCP server.

Holds the protocol-typed view of the parent :mod:`agentgrep` package along
with the shared constants and type aliases that the rest of the
``agentgrep.mcp`` subpackage consumes. The dynamic import here breaks a
circular import that would otherwise arise from ``agentgrep.__init__``
trying to load its own MCP subpackage during library setup.
"""

from __future__ import annotations

import importlib
import pathlib
import typing as t

AgentName = t.Literal["codex", "claude", "cursor", "gemini"]
AgentSelector = t.Literal["codex", "claude", "cursor", "gemini", "all"]
SearchTypeName = t.Literal["prompts", "history", "all"]

SERVER_VERSION = "0.1.0"
KNOWN_ADAPTERS: tuple[str, ...] = (
    "codex.history_json.v1",
    "codex.sessions_jsonl.v1",
    "claude.projects_jsonl.v1",
    "cursor.ai_tracking_sqlite.v1",
    "cursor.cli_jsonl.v1",
    "cursor.state_vscdb_legacy.v1",
    "cursor.state_vscdb_modern.v1",
    "gemini.tmp_chats_jsonl.v1",
    "gemini.tmp_chats_legacy_json.v1",
    "gemini.tmp_logs_json.v1",
)
READONLY_TAGS = {"readonly", "agentgrep"}
RESOURCE_ANNOTATIONS = {"readOnlyHint": True, "idempotentHint": True}


class SearchRecordLike(t.Protocol):
    """Structural type for shared ``agentgrep`` search records."""

    kind: str
    agent: str
    store: str
    adapter_id: str
    path: pathlib.Path
    text: str
    title: str | None
    role: str | None
    timestamp: str | None
    model: str | None
    session_id: str | None
    conversation_id: str | None
    metadata: dict[str, object]


class FindRecordLike(t.Protocol):
    """Structural type for shared ``agentgrep`` find records."""

    kind: str
    agent: str
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: str
    metadata: dict[str, object]


class SourceHandleLike(t.Protocol):
    """Structural type for discovered ``agentgrep`` sources."""

    agent: str
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: str
    source_kind: str
    search_root: pathlib.Path | None
    mtime_ns: int


class SearchQueryFactory(t.Protocol):
    """Factory protocol for ``agentgrep.SearchQuery``."""

    def __call__(
        self,
        *,
        terms: tuple[str, ...],
        search_type: str,
        any_term: bool,
        regex: bool,
        case_sensitive: bool,
        agents: tuple[str, ...],
        limit: int | None,
    ) -> object: ...


class BackendSelectionLike(t.Protocol):
    """Structural type for subprocess backend selection."""

    find_tool: str | None
    grep_tool: str | None
    json_tool: str | None


class AgentGrepModule(t.Protocol):
    """Structural type for the imported ``agentgrep`` module."""

    SCHEMA_VERSION: str
    AGENT_CHOICES: tuple[AgentName, ...]
    SearchQuery: SearchQueryFactory

    def parse_agents(self, values: list[str]) -> tuple[str, ...]: ...

    def select_backends(self) -> BackendSelectionLike: ...

    def discover_sources(
        self,
        home: pathlib.Path,
        agents: tuple[str, ...],
        backends: BackendSelectionLike,
    ) -> list[SourceHandleLike]: ...

    def run_search_query(
        self,
        home: pathlib.Path,
        query: object,
        *,
        backends: BackendSelectionLike | None = None,
    ) -> list[SearchRecordLike]: ...

    def run_find_query(
        self,
        home: pathlib.Path,
        agents: tuple[str, ...],
        *,
        pattern: str | None,
        limit: int | None,
        backends: BackendSelectionLike | None = None,
    ) -> list[FindRecordLike]: ...

    def serialize_search_record(
        self,
        record: SearchRecordLike,
    ) -> dict[str, object]: ...

    def serialize_find_record(
        self,
        record: FindRecordLike,
    ) -> dict[str, object]: ...

    def serialize_source_handle(
        self,
        source: SourceHandleLike,
    ) -> dict[str, object]: ...


agentgrep = t.cast(
    "AgentGrepModule",
    t.cast("object", importlib.import_module("agentgrep")),
)


def normalize_agent_selection(agent: AgentSelector) -> tuple[str, ...]:
    """Convert a single MCP agent selector into ``agentgrep`` agents."""
    values: list[str] = [] if agent == "all" else [agent]
    return agentgrep.parse_agents(values)
