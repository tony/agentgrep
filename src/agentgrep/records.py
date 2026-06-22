"""Domain record types and shared public vocabulary for agentgrep.

This module is the dependency-free root of the package's import graph: it
defines the normalized record dataclasses, their JSON payload ``TypedDict``
shapes, the public ``Literal`` and type aliases every surface shares, and the
small set of domain constants. It imports only the standard library and
:mod:`agentgrep.stores`; it must never import the engine, adapters, discovery,
or any frontend.
"""

from __future__ import annotations

import dataclasses
import pathlib
import typing as t

from agentgrep.stores import (
    PathKind,
    SourceKind,
    StoreCoverage,
    StoreRole,
    VersionDetectionConfidence,
    VersionDetectionStrategy,
)

if t.TYPE_CHECKING:
    from agentgrep.query.compile import CompiledQuery

__all__ = [
    "AGENT_CHOICES",
    "CONVERSATION_STORE_ROLES",
    "CURSOR_STATE_TOKENS",
    "ITER_SOURCE_RECORD_ADAPTERS",
    "JSON_FILE_SUFFIXES",
    "OFFICIAL_CURSOR_STATE_PATHS",
    "PROMPT_HISTORY_STORE_ROLES",
    "SCHEMA_VERSION",
    "USER_ROLES",
    "AgentName",
    "BackendSelection",
    "ColorMode",
    "DiscoveryRoot",
    "DiscoveryStoreRoles",
    "DiscoveryVersionContext",
    "DiscoveryVersionDetail",
    "EnvelopeFactory",
    "EnvelopePayload",
    "FindRecord",
    "FindRecordPayload",
    "FindSourceTypeFilter",
    "GrepStyle",
    "JSONScalar",
    "JSONValue",
    "KeyValueRow",
    "MessageCandidate",
    "OutputMode",
    "ProgressMode",
    "RawJsonlSkipLine",
    "SearchMatchSurface",
    "SearchQuery",
    "SearchRecord",
    "SearchRecordPayload",
    "SearchScope",
    "SourceHandle",
    "SourceHandlePayload",
    "SourceVersionDetection",
    "SourceVersionDetectionPayload",
    "SummaryRow",
]

# --- Public literals and type aliases -------------------------------------

AgentName = t.Literal[
    "codex",
    "claude",
    "cursor-cli",
    "cursor-ide",
    "gemini",
    "antigravity-cli",
    "antigravity-ide",
    "grok",
    "pi",
    "opencode",
    "windsurf",
    "vscode",
]
OutputMode = t.Literal["text", "json", "ndjson", "ui"]
ProgressMode = t.Literal["auto", "always", "never"]
SearchScope = t.Literal["prompts", "conversations", "all"]
SearchMatchSurface = t.Literal["haystack", "text"]
DiscoveryVersionDetail = t.Literal["none", "catalog", "shape"]
DiscoveryStoreRoles = frozenset[StoreRole] | None
ColorMode = t.Literal["auto", "always", "never"]
GrepStyle = t.Literal["default", "pretty"]
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type RawJsonlSkipLine = t.Callable[[str], bool]
type SummaryRow = tuple[object, object, object, object, object, object, object, object]
type KeyValueRow = tuple[object, object]
type DiscoveryRoot = pathlib.Path | tuple[pathlib.Path, ...]
type FindSourceTypeFilter = t.Literal["prompts", "history", "sessions", "all"]

# --- Domain constants ------------------------------------------------------

AGENT_CHOICES: tuple[AgentName, ...] = (
    "codex",
    "claude",
    "cursor-cli",
    "cursor-ide",
    "gemini",
    "antigravity-cli",
    "antigravity-ide",
    "grok",
    "pi",
    "opencode",
    "vscode",
)
JSON_FILE_SUFFIXES: frozenset[str] = frozenset({".json", ".jsonl"})
SCHEMA_VERSION: str = "agentgrep.v1"
USER_ROLES: frozenset[str] = frozenset({"human", "user"})
CURSOR_STATE_TOKENS: tuple[str, ...] = ("chat", "composer", "prompt", "history")
OFFICIAL_CURSOR_STATE_PATHS: tuple[pathlib.Path, ...] = (
    pathlib.Path("~/.config/Cursor/User/globalStorage/state.vscdb").expanduser(),
    pathlib.Path(
        "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
    ).expanduser(),
    pathlib.Path("~/AppData/Roaming/Cursor/User/globalStorage/state.vscdb").expanduser(),
)
ITER_SOURCE_RECORD_ADAPTERS: frozenset[str] = frozenset(
    {
        "claude.history_jsonl.v1",
        "antigravity_cli.brain_text.v1",
        "antigravity_cli.conversations_sqlite_protobuf.v1",
        "antigravity_cli.history_jsonl.v1",
        "antigravity_cli.implicit_protobuf.v1",
        "antigravity_cli.transcript_jsonl.v1",
        "antigravity_ide.brain_text.v1",
        "antigravity_ide.brain_resolved_text.v1",
        "antigravity_ide.conversations_protobuf.v1",
        "antigravity_ide.implicit_protobuf.v1",
        "antigravity_ide.skills_text.v1",
        "claude.app_state_json_summary.v1",
        "claude.commands_text.v1",
        "claude.file_metadata_summary.v1",
        "claude.memory_text.v1",
        "claude.plans_text.v1",
        "claude.plugin_hooks_json.v1",
        "claude.plugin_instruction_text.v1",
        "claude.plugin_manifest_json.v1",
        "claude.project_instruction_text.v1",
        "claude.projects_memory_text.v1",
        "claude.projects_jsonl.v1",
        "claude.session_memory_text.v1",
        "claude.settings_json.v1",
        "claude.skills_text.v1",
        "claude.store_sqlite.v1",
        "claude.usage_facets_json.v1",
        "claude.tasks_json.v1",
        "claude.teams_json.v1",
        "claude.todos_json.v1",
        "claude.workflow_scripts_text.v1",
        "codex.app_state_json_summary.v1",
        "codex.config_backup_toml.v1",
        "codex.config_toml.v1",
        "codex.external_imports_json.v1",
        "codex.file_metadata_summary.v1",
        "codex.goals_sqlite.v1",
        "codex.hooks_json.v1",
        "codex.history_json.v1",
        "codex.history_jsonl.v1",
        "codex.instructions_text.v1",
        "codex.logs_sqlite.v1",
        "codex.memories_sqlite.v1",
        "codex.memories_text.v1",
        "codex.plugin_hooks_json.v1",
        "codex.plugin_instruction_text.v1",
        "codex.plugin_manifest_json.v1",
        "codex.plugin_marketplace_json.v1",
        "codex.project_config_toml.v1",
        "codex.project_skill_text.v1",
        "codex.rules_text.v1",
        "codex.session_index_jsonl.v1",
        "codex.sessions_jsonl.v1",
        "codex.sessions_legacy_json.v1",
        "codex.skills_text.v1",
        "codex.state_sqlite.v1",
        "cursor_cli.ai_tracking_sqlite.v1",
        "cursor_cli.chats_protobuf.v1",
        "cursor_cli.prompt_history_json.v1",
        "cursor_cli.skills_text.v1",
        "cursor_cli.uploads_text.v1",
        "cursor_cli.agent_tools_text.v1",
        "cursor_cli.transcripts_jsonl.v1",
        "cursor_ide.state_vscdb_legacy.v1",
        "cursor_ide.state_vscdb_modern.v1",
        "gemini.tmp_chats_jsonl.v1",
        "gemini.tmp_chats_legacy_json.v1",
        "gemini.tmp_logs_json.v1",
        "gemini.memory_text.v1",
        "gemini.tool_outputs_text.v1",
        "grok.prompt_history_jsonl.v1",
        "grok.session_search_sqlite.v1",
        "grok.sessions_jsonl.v1",
        "grok.subagents_json.v1",
        "grok.plans_text.v1",
        "grok.memory_text.v1",
        "pi.sessions_jsonl.v1",
        "pi.context_mode_sqlite.v1",
        "opencode.db_sqlite.v1",
        "vscode.chat_sessions_json.v1",
        "vscode.inline_history_sqlite.v1",
    },
)
EnvelopeFactory = t.Callable[[str, dict[str, object], list[dict[str, object]]], dict[str, object]]

# --- JSON payload shapes ---------------------------------------------------


class SearchRecordPayload(t.TypedDict):
    """JSON payload for search records."""

    schema_version: str
    kind: t.Literal["prompt", "history"]
    agent: AgentName
    store: str
    adapter_id: str
    path: str
    text: str
    title: str | None
    role: str | None
    timestamp: str | None
    model: str | None
    session_id: str | None
    conversation_id: str | None
    metadata: dict[str, object]


class FindRecordPayload(t.TypedDict):
    """JSON payload for find records."""

    schema_version: str
    kind: t.Literal["find"]
    agent: AgentName
    store: str
    adapter_id: str
    path: str
    path_kind: PathKind
    metadata: dict[str, object]


class SourceHandlePayload(t.TypedDict):
    """JSON payload for discovered sources."""

    schema_version: str
    agent: AgentName
    store: str
    adapter_id: str
    path: str
    path_kind: PathKind
    source_kind: SourceKind
    coverage: StoreCoverage
    version_detection: SourceVersionDetectionPayload | None
    search_root: str | None
    mtime_ns: int


class EnvelopePayload(t.TypedDict):
    """JSON payload for top-level envelopes."""

    schema_version: str
    command: str
    query: dict[str, object]
    results: list[dict[str, object]]


class SourceVersionDetectionPayload(t.TypedDict):
    """JSON payload for source version detection metadata."""

    app_version: str | None
    data_version: str | None
    strategy: VersionDetectionStrategy
    confidence: VersionDetectionConfidence
    evidence: str


# --- Domain dataclasses ----------------------------------------------------


@dataclasses.dataclass(slots=True)
class BackendSelection:
    """Selected optional subprocess backends."""

    find_tool: str | None
    grep_tool: str | None
    json_tool: str | None


@dataclasses.dataclass(slots=True)
class SearchQuery:
    """Compiled search configuration.

    ``compiled`` carries the parsed-query predicates from
    :mod:`agentgrep.query`. When ``None`` (the default), the engine
    takes its legacy code path — pure-text queries and flag-only
    invocations stay on the fast path with no extra evaluation
    cost. When set, ``iter_search_events`` consults
    ``compiled.source_predicate`` to prune sources before any file
    is opened, and :func:`matches_record` consults
    ``compiled.record_predicate`` after the existing text match.
    ``match_surface`` lets line-oriented callers such as ``grep``
    require a match in record text while fuzzy search and filtering
    can keep using the metadata-rich haystack.
    """

    terms: tuple[str, ...]
    scope: SearchScope
    any_term: bool
    regex: bool
    case_sensitive: bool
    agents: tuple[AgentName, ...]
    limit: int | None
    dedupe: bool = True
    compiled: CompiledQuery | None = None
    match_surface: SearchMatchSurface = "haystack"


@dataclasses.dataclass(slots=True)
class SourceVersionDetection:
    """Detected app/data version metadata for one concrete source."""

    app_version: str | None
    data_version: str | None
    strategy: VersionDetectionStrategy
    confidence: VersionDetectionConfidence
    evidence: str


@dataclasses.dataclass(slots=True)
class DiscoveryVersionContext:
    """Cached metadata shared across one source-discovery pass."""

    codex_client_version: str | None = None


@dataclasses.dataclass(slots=True)
class SourceHandle:
    """A discovered, parseable source file or SQLite database."""

    agent: AgentName
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: PathKind
    source_kind: SourceKind
    search_root: pathlib.Path | None
    mtime_ns: int
    coverage: StoreCoverage = StoreCoverage.DEFAULT_SEARCH
    version_detection: SourceVersionDetection | None = None


@dataclasses.dataclass(slots=True)
class SearchRecord:
    """Normalized prompt/history record."""

    kind: t.Literal["prompt", "history"]
    agent: AgentName
    store: str
    adapter_id: str
    path: pathlib.Path
    text: str
    title: str | None = None
    role: str | None = None
    timestamp: str | None = None
    model: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class FindRecord:
    """Normalized discovery record for ``agentgrep find``."""

    kind: t.Literal["find"]
    agent: AgentName
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: PathKind
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class MessageCandidate:
    """Intermediate parsed message representation."""

    role: str | None
    text: str
    title: str | None = None
    timestamp: str | None = None
    model: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None


# --- Store-role classification constants -----------------------------------

PROMPT_HISTORY_STORE_ROLES: frozenset[StoreRole] = frozenset({StoreRole.PROMPT_HISTORY})

CONVERSATION_STORE_ROLES: frozenset[StoreRole] = frozenset(
    {StoreRole.PRIMARY_CHAT, StoreRole.SUPPLEMENTARY_CHAT},
)
