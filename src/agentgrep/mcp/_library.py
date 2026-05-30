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

AgentName = t.Literal["codex", "claude", "cursor-cli", "cursor-ide", "gemini", "grok", "pi"]
AgentSelector = t.Literal[
    "codex", "claude", "cursor-cli", "cursor-ide", "gemini", "grok", "pi", "all"
]
SearchTypeName = t.Literal["prompts", "history", "all"]

SERVER_VERSION = "0.1.0"
KNOWN_ADAPTERS: tuple[str, ...] = (
    "codex.history_json.v1",
    "codex.history_jsonl.v1",
    "codex.session_index_jsonl.v1",
    "codex.sessions_jsonl.v1",
    "codex.sessions_legacy_json.v1",
    "codex.state_sqlite.v1",
    "codex.logs_sqlite.v1",
    "codex.memories_sqlite.v1",
    "codex.memories_text.v1",
    "codex.goals_sqlite.v1",
    "codex.external_imports_json.v1",
    "codex.instructions_text.v1",
    "codex.app_state_json_summary.v1",
    "codex.config_backup_toml.v1",
    "codex.config_toml.v1",
    "codex.file_metadata_summary.v1",
    "codex.hooks_json.v1",
    "codex.plugin_hooks_json.v1",
    "codex.plugin_instruction_text.v1",
    "codex.plugin_manifest_json.v1",
    "codex.plugin_marketplace_json.v1",
    "codex.project_config_toml.v1",
    "codex.project_skill_text.v1",
    "codex.rules_text.v1",
    "codex.skills_text.v1",
    "claude.history_jsonl.v1",
    "claude.projects_jsonl.v1",
    "claude.projects_memory_text.v1",
    "claude.store_sqlite.v1",
    "claude.tasks_json.v1",
    "claude.todos_json.v1",
    "claude.teams_json.v1",
    "claude.plans_text.v1",
    "claude.session_memory_text.v1",
    "claude.settings_json.v1",
    "claude.skills_text.v1",
    "claude.commands_text.v1",
    "claude.app_state_json_summary.v1",
    "claude.file_metadata_summary.v1",
    "claude.memory_text.v1",
    "claude.plugin_hooks_json.v1",
    "claude.plugin_instruction_text.v1",
    "claude.plugin_manifest_json.v1",
    "claude.project_instruction_text.v1",
    "cursor_cli.ai_tracking_sqlite.v1",
    "cursor_cli.chats_protobuf.v1",
    "cursor_cli.prompt_history_json.v1",
    "cursor_cli.transcripts_jsonl.v1",
    "cursor_ide.state_vscdb_legacy.v1",
    "cursor_ide.state_vscdb_modern.v1",
    "gemini.tmp_chats_jsonl.v1",
    "gemini.tmp_chats_legacy_json.v1",
    "gemini.tmp_logs_json.v1",
    "grok.prompt_history_jsonl.v1",
    "grok.sessions_jsonl.v1",
    "grok.session_search_sqlite.v1",
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
    coverage: str
    version_detection: object | None
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
        *,
        include_non_default: bool = False,
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

    def matches_text(
        self,
        text: str,
        query: object,
    ) -> bool: ...

    def iter_source_records(
        self,
        source: SourceHandleLike,
    ) -> t.Iterator[SearchRecordLike]: ...


agentgrep = t.cast(
    "AgentGrepModule",
    t.cast("object", importlib.import_module("agentgrep")),
)


def normalize_agent_selection(agent: AgentSelector) -> tuple[str, ...]:
    """Convert a single MCP agent selector into ``agentgrep`` agents."""
    values: list[str] = [] if agent == "all" else [agent]
    return agentgrep.parse_agents(values)
