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
    "CONVERSATION_CONTENT_STORES",
    "CONVERSATION_STORE_ROLES",
    "CURSOR_STATE_TOKENS",
    "DEFAULT_TARGETED_CONVERSATION_LIMIT",
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
    "RecordIdStability",
    "RecordOrigin",
    "RecordOriginPayload",
    "RecordPosition",
    "SearchEffort",
    "SearchMatchSurface",
    "SearchQuery",
    "SearchRecord",
    "SearchRecordPayload",
    "SearchScope",
    "SearchScopeProvenance",
    "SourceHandle",
    "SourceHandlePayload",
    "SourceOriginSummary",
    "SourceScanOutcome",
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
SearchScopeProvenance = t.Literal["inferred", "explicit"]
SearchEffort = t.Literal["prompt", "targeted", "exhaustive"]
SearchMatchSurface = t.Literal["haystack", "text"]
SourceScanOutcome = t.Literal[
    "completed",
    "bounded",
    "unsupported",
    "failed",
    "cancelled",
]
DiscoveryVersionDetail = t.Literal["none", "catalog", "shape"]
DiscoveryStoreRoles = frozenset[StoreRole] | None
ColorMode = t.Literal["auto", "always", "never"]
GrepStyle = t.Literal["default", "pretty"]
DEFAULT_TARGETED_CONVERSATION_LIMIT = 25
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]
type RawJsonlSkipLine = t.Callable[[str], bool]
type SummaryRow = tuple[object, object, object, object, object, object, object, object]
type KeyValueRow = tuple[object, object]
type DiscoveryRoot = pathlib.Path | tuple[pathlib.Path, ...]
type FindSourceTypeFilter = t.Literal["prompts", "history", "sessions", "all"]
type RecordIdStability = t.Literal["native", "source_order"]

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
        "antigravity_cli.transcript_jsonl.v1",
        "antigravity_ide.brain_text.v1",
        "antigravity_ide.brain_resolved_text.v1",
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
    """JSON payload for search records.

    Mirrors :class:`SearchRecord` on the wire, with paths rewritten to their display form.

    Attributes
    ----------
    schema_version : str
        :data:`SCHEMA_VERSION` stamped on every emitted payload so a reader can tell which
        wire shape it holds.
    kind : t.Literal["prompt", "history"]
        ``"prompt"`` when ``role`` is a user role, ``"history"`` for everything else the
        transcript holds.
    agent : AgentName
        Agent that owns the store this record came from.
    store : str
        Runtime store key the record was read from.
    adapter_id : str
        Versioned parser identity that produced the record.
    path : str
        Display form of the source path, with the user's home abbreviated to ``~``.
    text : str
        Message body, and the text term matching ran against.
    title : str | None
        Session or conversation title. ``None`` when the store names none.
    role : str | None
        Speaker label as the store spelled it, e.g. ``"user"`` or ``"assistant"``, kept
        uncased. ``None`` when the store records no role.
    timestamp : str | None
        ISO 8601 time the message was recorded. ``None`` when the store records none.
    model : str | None
        Model credited with the message. ``None`` when the store records none.
    session_id : str | None
        Store's identifier for the session. ``None`` when the store records none.
    conversation_id : str | None
        Store's identifier for the conversation or thread. ``None`` when the store records
        none.
    origin : RecordOriginPayload | None
        Project the record came from. ``None`` when nothing was recorded, recovered, or
        left after display rewriting.
    metadata : dict[str, object]
        Adapter-specific extras with no normalized field of their own, with path-like
        legacy origin values rewritten for display.
    """

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
    content_id: str
    record_id: str | None
    record_id_stability: RecordIdStability | None
    thread_id: str | None
    origin: RecordOriginPayload | None
    metadata: dict[str, object]


class RecordOriginPayload(t.TypedDict, total=False):
    """JSON payload for project-origin metadata.

    Mirrors :class:`RecordOrigin` on the wire. Every key is optional: a field the origin
    left ``None`` is omitted rather than emitted as null, and an origin with no keys left
    is emitted as ``None`` instead of an empty object.

    Attributes
    ----------
    cwd : str
        Display form of the working directory the session ran in.
    repo : str
        Display form of the repository root the session belonged to.
    worktree : str
        Display form of the checkout directory when the session ran in a git worktree.
    branch : str
        Branch checked out during the session.
    remote : str
        Repository remote normalized to a scheme/host/path URL. A remote carrying
        credentials or an unrecognized scheme is omitted rather than rewritten.
    cwd_hash : str
        Digest a store derived from the working-directory path and used as a directory
        name.
    """

    cwd: str
    repo: str
    worktree: str
    branch: str
    remote: str
    cwd_hash: str


class FindRecordPayload(t.TypedDict):
    """JSON payload for find records.

    Mirrors :class:`FindRecord` on the wire, with paths rewritten to their display form.

    Attributes
    ----------
    schema_version : str
        :data:`SCHEMA_VERSION` stamped on every emitted payload so a reader can tell which
        wire shape it holds.
    kind : t.Literal["find"]
        Constant tag marking a discovered source rather than a message.
    agent : AgentName
        Agent that owns the store this source belongs to.
    store : str
        Runtime store key the source belongs to.
    adapter_id : str
        Versioned parser identity that would read this source.
    path : str
        Display form of the discovered path, with the user's home abbreviated to ``~``.
    path_kind : PathKind
        Filesystem entry the records live in.
    metadata : dict[str, object]
        Discovery extras, such as the source's parse format.
    """

    schema_version: str
    kind: t.Literal["find"]
    agent: AgentName
    store: str
    adapter_id: str
    path: str
    path_kind: PathKind
    metadata: dict[str, object]


class SourceHandlePayload(t.TypedDict):
    """JSON payload for discovered sources.

    Mirrors the wire-facing part of :class:`SourceHandle`; ``origin_summary`` stays
    internal to the engine and is not emitted.

    Attributes
    ----------
    schema_version : str
        :data:`SCHEMA_VERSION` stamped on every emitted payload so a reader can tell which
        wire shape it holds.
    agent : AgentName
        Agent that owns the store this source belongs to.
    store : str
        Runtime store key, e.g. ``"claude.projects"``.
    adapter_id : str
        Versioned parser identity for this source, e.g. ``"claude.projects_jsonl.v1"``.
    path : str
        Display form of the source path, with the user's home abbreviated to ``~``.
    path_kind : PathKind
        Filesystem entry the records live in.
    source_kind : SourceKind
        Parse format the adapter applies to the bytes.
    coverage : StoreCoverage
        Runtime search policy for the store, deciding which scopes open this source.
    version_detection : SourceVersionDetectionPayload | None
        Detected app/data version. ``None`` when discovery skipped detection or learned
        nothing.
    search_root : str | None
        Display form of the directory the glob that found ``path`` was walked under, with
        a trailing separator. ``None`` for sources named by an exact filename.
    mtime_ns : int
        Modification time in nanoseconds, used for recency ordering and as a timestamp of
        last resort for stores that record none.
    """

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
    """JSON payload for top-level envelopes.

    The outermost object the ``--json`` output mode writes: one envelope wrapping the
    record payloads a single command produced.

    Attributes
    ----------
    schema_version : str
        :data:`SCHEMA_VERSION` stamped on every emitted payload so a reader can tell which
        wire shape it holds.
    command : str
        Subcommand that produced the results, e.g. ``"search"`` or ``"find"``.
    query : dict[str, object]
        Request the results answer, echoed back so a stored envelope stays self-describing.
    results : list[dict[str, object]]
        Serialized record payloads in emit order. Empty when nothing matched.
    """

    schema_version: str
    command: str
    query: dict[str, object]
    results: list[dict[str, object]]


class SourceVersionDetectionPayload(t.TypedDict):
    """JSON payload for source version detection metadata.

    Mirrors :class:`SourceVersionDetection` on the wire.

    Attributes
    ----------
    app_version : str | None
        Version of the agent that wrote the source. ``None`` when the detection pinned a
        data shape but learned no application version.
    data_version : str | None
        Version of the on-disk record shape the adapter parses. ``None`` when the shape
        was not pinned.
    strategy : VersionDetectionStrategy
        How the version was learned — a version probe, metadata embedded in the source,
        inference from the record shape, or the catalogue's observed version.
    confidence : VersionDetectionConfidence
        How much weight the detection carries.
    evidence : str
        Short note naming what was inspected, such as the object keys that decided a
        shape.
    """

    app_version: str | None
    data_version: str | None
    strategy: VersionDetectionStrategy
    confidence: VersionDetectionConfidence
    evidence: str


# --- Domain dataclasses ----------------------------------------------------


@dataclasses.dataclass(slots=True)
class BackendSelection:
    """Selected optional subprocess backends.

    Attributes
    ----------
    find_tool : str | None
        Resolved executable for directory walks (``fd`` or ``fdfind``). ``None`` when
        neither is on ``PATH``, leaving the pure-Python walk in charge.
    grep_tool : str | None
        Resolved executable for the root text prefilter (``rg`` or ``ag``). ``None`` when
        neither is on ``PATH``, so every candidate source is opened and scanned.
    json_tool : str | None
        Resolved executable for JSON and JSONL prefiltering (``jq`` or ``jaq``). ``None``
        when neither is on ``PATH``.
    """

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
    ``origin_filter`` carries explicit CLI/MCP project filters outside
    the compiled query so plain text searches keep the legacy fast path.

    Attributes
    ----------
    terms : tuple[str, ...]
        Text needles a record must match. Empty admits every record the remaining
        filters allow.
    scope : SearchScope
        Which record kinds may be returned: prompts, conversations, or both.
    any_term : bool
        Whether one matching term suffices. ``False`` requires every term to match.
    regex : bool
        Whether each term is a regular expression rather than a literal substring.
    case_sensitive : bool
        Whether matching respects case. ``False`` folds case on both sides.
    agents : tuple[AgentName, ...]
        Agents whose stores are discovered. An empty tuple discovers nothing; callers
        meaning "every agent" pass :data:`AGENT_CHOICES`.
    limit : int | None
        Result ceiling. Scan-ordered plans may stop early when their source policy
        proves the remaining work irrelevant; globally ordered plans compare every
        eligible match. ``None`` returns every accepted match.
    dedupe : bool
        Whether records that collapse to one identity are folded together. ``False``
        keeps every match, including the same message reached through two stores.
    compiled : CompiledQuery | None
        Parsed-query predicates from :mod:`agentgrep.query`. ``None`` on plain-text and
        flag-only invocations, which skip predicate evaluation entirely.
    match_surface : SearchMatchSurface
        Text a term must land in: ``"haystack"`` accepts the metadata-rich surface,
        ``"text"`` restricts matching to record text.
    origin_filter : RecordOrigin | None
        Project filter supplied by the CLI or MCP surface, held apart from ``compiled``.
        ``None`` applies no origin filter.
    effort : SearchEffort | None
        Read policy for source admission. ``"prompt"`` opens only dedicated prompt
        history, ``"targeted"`` adds a bounded set of prompt-routed conversations,
        and ``"exhaustive"`` admits every eligible transcript backend. ``None``
        preserves compatibility by deriving the policy from ``scope``.
    conversation_limit : int | None
        Maximum distinct conversation attempts for targeted effort. ``None`` uses
        :data:`DEFAULT_TARGETED_CONVERSATION_LIMIT`; other efforts leave it unused.
    order : str
        Result order requested from the engine. ``"newest"`` is the public default;
        ``"relevance"`` ranks matches before applying the result limit; ``"scan"``
        permits count-bounded execution when a caller does not require a global order.
    relevance_threshold : int
        Minimum relevance score retained when ``order`` is ``"relevance"``.
    origin_boost : RecordOrigin | None
        Optional project context whose matching records receive a relevance boost.
    scope_provenance : SearchScopeProvenance
        Whether scope was inferred from defaults/query semantics or explicitly selected.
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
    origin_filter: RecordOrigin | None = None
    effort: SearchEffort | None = None
    order: str = "newest"
    scope_provenance: SearchScopeProvenance = "inferred"
    conversation_limit: int | None = None
    relevance_threshold: int = 0
    origin_boost: RecordOrigin | None = None


@dataclasses.dataclass(slots=True)
class SourceVersionDetection:
    """Detected app/data version metadata for one concrete source.

    Attributes
    ----------
    app_version : str | None
        Version of the agent that wrote the source. ``None`` when the detection pinned a
        data shape but learned no application version.
    data_version : str | None
        Version of the on-disk record shape the adapter parses. ``None`` when the shape
        was not pinned.
    strategy : VersionDetectionStrategy
        How the version was learned — a version probe, metadata embedded in the source,
        inference from the record shape, or the catalogue's observed version.
    confidence : VersionDetectionConfidence
        How much weight the detection carries.
    evidence : str
        Short note naming what was inspected, such as the object keys that decided a
        shape.
    """

    app_version: str | None
    data_version: str | None
    strategy: VersionDetectionStrategy
    confidence: VersionDetectionConfidence
    evidence: str


@dataclasses.dataclass(slots=True)
class DiscoveryVersionContext:
    """Cached metadata shared across one source-discovery pass.

    Attributes
    ----------
    codex_client_version : str | None
        Codex client version read once from its local models cache and reused for every
        Codex source in the pass. ``None`` when the cache is absent or names no version.
    """

    codex_client_version: str | None = None


@dataclasses.dataclass(slots=True)
class SourceHandle:
    """A discovered, parseable source file or SQLite database.

    Attributes
    ----------
    agent : AgentName
        Agent that owns the store this source belongs to.
    store : str
        Runtime store key, e.g. ``"claude.projects"``.
    adapter_id : str
        Versioned parser identity for this source, e.g. ``"claude.projects_jsonl.v1"``.
    path : pathlib.Path
        Absolute path to the file or database records are read from.
    path_kind : PathKind
        Filesystem entry the records live in.
    source_kind : SourceKind
        Parse format the adapter applies to the bytes.
    search_root : pathlib.Path | None
        Directory the glob that found ``path`` was walked under, so one text prefilter can
        cover every sibling beneath it. ``None`` for sources named by an exact filename.
    mtime_ns : int
        Modification time in nanoseconds, used for recency ordering and as a timestamp of
        last resort for stores that record none.
    coverage : StoreCoverage
        Runtime search policy for the store, deciding which scopes open this source.
    version_detection : SourceVersionDetection | None
        Detected app/data version. ``None`` when discovery skipped detection or learned
        nothing.
    origin_summary : SourceOriginSummary | None
        Origin facts known from discovery metadata alone, letting the query layer drop the
        source before opening it. ``None`` when discovery learned none.
    """

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
    origin_summary: SourceOriginSummary | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class RecordPosition:
    """Backend-native or source-order position of one normalized record."""

    native_id: str | None = None
    parent_native_id: str | None = None
    ordinal: int | None = None
    quality: RecordIdStability | None = None


@dataclasses.dataclass(slots=True)
class SearchRecord:
    """Normalized prompt/history record.

    Attributes
    ----------
    kind : t.Literal["prompt", "history"]
        ``"prompt"`` when ``role`` is a user role, ``"history"`` for everything else the
        transcript holds.
    agent : AgentName
        Agent that owns the store this record came from.
    store : str
        Runtime store key the record was read from.
    adapter_id : str
        Versioned parser identity that produced the record.
    path : pathlib.Path
        Absolute path to the file or database the record was read from.
    text : str
        Message body, and the text term matching runs against.
    title : str | None
        Session or conversation title. ``None`` when the store names none.
    role : str | None
        Speaker label as the store spelled it, e.g. ``"user"`` or ``"assistant"``, kept
        uncased. ``None`` when the store records no role.
    timestamp : str | None
        ISO 8601 time the message was recorded. ``None`` when the store records none.
    model : str | None
        Model credited with the message. ``None`` when the store records none.
    session_id : str | None
        Store's identifier for the session. ``None`` when the store records none.
    conversation_id : str | None
        Store's identifier for the conversation or thread. ``None`` when the store records
        none.
    metadata : dict[str, object]
        Adapter-specific extras with no normalized field of their own.
    origin : RecordOrigin | None
        Project the record came from, held out of the text haystack so origin filters do
        not shift ordinary relevance. ``None`` when nothing was recorded or recovered.
    """

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
    origin: RecordOrigin | None = None
    identity_namespace: str | None = None
    position: RecordPosition | None = None


@dataclasses.dataclass(slots=True)
class FindRecord:
    """Normalized discovery record for ``agentgrep find``.

    Attributes
    ----------
    kind : t.Literal["find"]
        Constant tag marking a discovered source rather than a message.
    agent : AgentName
        Agent that owns the store this source belongs to.
    store : str
        Runtime store key the source belongs to.
    adapter_id : str
        Versioned parser identity that would read this source.
    path : pathlib.Path
        Absolute path to the discovered file or database.
    path_kind : PathKind
        Filesystem entry the records live in.
    metadata : dict[str, object]
        Discovery extras, such as the source's parse format.
    """

    kind: t.Literal["find"]
    agent: AgentName
    store: str
    adapter_id: str
    path: pathlib.Path
    path_kind: PathKind
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class MessageCandidate:
    """Intermediate parsed message representation.

    An adapter fills one of these per parsed message; the record layer pairs it with the
    owning :class:`SourceHandle` to build a :class:`SearchRecord`.

    Attributes
    ----------
    role : str | None
        Speaker label as the store spelled it. Its case-folded form decides whether the
        record becomes a prompt or history. ``None`` when the store records no role.
    text : str
        Message body carried through to the record's searched text.
    title : str | None
        Session or conversation title. ``None`` when the store names none.
    timestamp : str | None
        ISO 8601 time the message was recorded. ``None`` when the store records none.
    model : str | None
        Model credited with the message. ``None`` when the store records none.
    session_id : str | None
        Store's identifier for the session. ``None`` when the store records none.
    conversation_id : str | None
        Store's identifier for the conversation or thread. ``None`` when the store records
        none.
    origin : RecordOrigin | None
        Project the message came from. ``None`` when nothing was recorded or recovered.
    """

    role: str | None
    text: str
    title: str | None = None
    timestamp: str | None = None
    model: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    origin: RecordOrigin | None = None
    identity_namespace: str | None = None
    position: RecordPosition | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class RecordOrigin:
    """Project/workspace origin attached to a normalized record.

    Every field is independently optional: a store may record a branch and nothing else,
    or only a digest of a directory it never wrote out. All-``None`` is the empty origin
    :meth:`is_empty` reports.

    Attributes
    ----------
    cwd : str | None
        Working directory the session ran in, either as the store recorded it or as
        recovered from a directory name the store encoded it into. ``None`` when unknown.
    repo : str | None
        Repository root the session belonged to. ``None`` when unknown.
    worktree : str | None
        Checkout directory when the session ran in a git worktree. ``None`` when unknown.
    branch : str | None
        Branch checked out during the session. ``None`` when unknown.
    remote : str | None
        Repository remote the session's checkout pointed at. ``None`` when unknown.
    cwd_hash : str | None
        Digest a store derived from the working-directory path and used as a directory
        name. Only ever the digest a store itself wrote, never one synthesized by hashing
        a recovered ``cwd``. ``None`` when the store wrote none.
    """

    cwd: str | None = None
    repo: str | None = None
    worktree: str | None = None
    branch: str | None = None
    remote: str | None = None
    cwd_hash: str | None = None

    def is_empty(self) -> bool:
        """Return whether this origin carries no useful project signal."""
        return not any(
            (
                self.cwd,
                self.repo,
                self.worktree,
                self.branch,
                self.remote,
                self.cwd_hash,
            ),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class SourceOriginSummary:
    """Source-level origin facts safe for conservative pruning.

    Attributes
    ----------
    origins : tuple[RecordOrigin, ...]
        Origins discovery learned from the source's own location and sibling metadata,
        without opening it. Empty means no origin was recovered.
    complete_fields : frozenset[str]
        Fields of ``origins`` the summary claims cover every record in the source, letting
        a query drop the source unread. A field belongs here only when a parser cannot
        contradict it from inside the payload; see
        :data:`~agentgrep.origin.PRUNABLE_ORIGIN_FIELDS`. Empty claims nothing, so the
        source is always opened.
    """

    origins: tuple[RecordOrigin, ...] = ()
    complete_fields: frozenset[str] = frozenset()


# --- Store-role classification constants -----------------------------------

PROMPT_HISTORY_STORE_ROLES: frozenset[StoreRole] = frozenset({StoreRole.PROMPT_HISTORY})

CONVERSATION_STORE_ROLES: frozenset[StoreRole] = frozenset(
    {StoreRole.PRIMARY_CHAT, StoreRole.SUPPLEMENTARY_CHAT},
)

CONVERSATION_CONTENT_STORES: frozenset[str] = frozenset(
    {
        "codex.state_db",
        "pi.context_mode_db",
    },
)
"""Stores admitted at conversation scope despite carrying an app-state role.

Role describes what a store *is*: both of these are agent-owned SQLite state,
and reclassifying them would misdescribe them and drag every other app-state row
along with them. But both hold conversation content — Codex's ``threads`` table
indexes every Codex thread, Pi's context-mode events are session turns — so
``--scope conversations`` has to be able to reach them. An explicit allowlist
keeps each admission reviewable per store instead of implied by a role, and
keeps config files, shell snapshots, and debug logs out.

Membership is keyed on the *runtime* store name, which is
:attr:`agentgrep.stores.DiscoverySpec.store` (what
:attr:`agentgrep.records.SourceHandle.store` carries) rather than
:attr:`agentgrep.stores.StoreDescriptor.store_id`. Six catalogue rows give those
two different strings, so an allowlist keyed on one and consulted with the other
would silently admit nothing; ``tests/test_stores.py`` pins that the members
agree on both names.
"""
