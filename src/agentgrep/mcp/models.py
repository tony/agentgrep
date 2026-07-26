"""Pydantic models for ``agentgrep`` MCP tool inputs and outputs."""

from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from agentgrep.mcp._library import (
    SERVER_VERSION,
    AgentSelector,
    CatalogAgentSelector,
    FindRecordLike,
    SearchRecordLike,
    SearchScopeName,
    SourceHandleLike,
    agentgrep,
)


class AgentGrepModel(BaseModel):
    """Base model for MCP payloads.

    Attributes
    ----------
    model_config : t.ClassVar[ConfigDict]
        Pydantic settings shared by every MCP payload. ``extra="forbid"`` turns an
        unrecognized key into a validation error, so a client typo is reported rather
        than silently dropped.
    """

    model_config: t.ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class RecordOriginModel(AgentGrepModel):
    """Project/workspace origin attached to a search result.

    Every field is independently optional: a store may record a branch and nothing
    else, or only a digest of a directory it never wrote out. Paths arrive in display
    form, with the user's home abbreviated to ``~``.

    Attributes
    ----------
    cwd : str | None
        Working directory the session ran in, either as the store recorded it or as
        recovered from a directory name the store encoded it into. ``None`` when
        unknown.
    repo : str | None
        Repository root the session belonged to. ``None`` when unknown.
    worktree : str | None
        Checkout directory when the session ran in a git worktree. ``None`` when
        unknown.
    branch : str | None
        Branch checked out during the session. ``None`` when unknown.
    remote : str | None
        Repository remote normalized to a scheme/host/path URL. ``None`` when unknown,
        and a remote carrying credentials or an unrecognized scheme is dropped rather
        than rewritten.
    cwd_hash : str | None
        Digest a store derived from the working-directory path and used as a directory
        name. Only ever the digest a store itself wrote, never one synthesized from a
        recovered ``cwd``. ``None`` when the store wrote none.
    """

    cwd: str | None = None
    repo: str | None = None
    worktree: str | None = None
    branch: str | None = None
    remote: str | None = None
    cwd_hash: str | None = None


class SearchRecordModel(AgentGrepModel):
    """Normalized search result payload.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every emitted record so a reader can tell
        which wire shape it holds.
    ref : str
        Opaque handle for this record, passed to ``inspect_result`` to re-read it
        without the caller rebuilding a local path.
    kind : t.Literal["prompt", "history"]
        ``"prompt"`` when ``role`` is a user role, ``"history"`` for everything else the
        transcript holds.
    agent
        Agent that owns the store this record came from.
    store : str
        Runtime store key the record was read from, e.g. ``"claude.projects"``.
    adapter_id : str
        Versioned parser identity that produced the record, e.g.
        ``"claude.projects_jsonl.v1"``.
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
        Store's identifier for the conversation or thread. ``None`` when the store
        records none.
    origin : RecordOriginModel | None
        Project the record came from. ``None`` when nothing was recorded, recovered, or
        left after display rewriting.
    metadata : dict[str, t.Any]
        Adapter-specific extras with no normalized field of their own. Empty when the
        adapter recorded none.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    ref: str
    kind: t.Literal["prompt", "history"]
    agent: t.Literal[
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
    store: str
    adapter_id: str
    path: str
    text: str
    title: str | None = None
    role: str | None = None
    timestamp: str | None = None
    model: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    origin: RecordOriginModel | None = None
    metadata: dict[str, t.Any] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: SearchRecordLike) -> SearchRecordModel:
        """Build a typed result from an ``agentgrep`` search record."""
        from agentgrep.mcp import refs

        payload = agentgrep.serialize_search_record(record)
        payload["ref"] = refs.make_search_ref(record)
        return cls.model_validate(payload)


class FindRecordModel(AgentGrepModel):
    """Normalized find result payload.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every emitted record so a reader can tell
        which wire shape it holds.
    ref : str
        Opaque handle for this source, passed to ``inspect_result`` to read records
        from it without the caller rebuilding a local path.
    kind : t.Literal["find"]
        Constant tag marking a discovered source rather than a message.
    agent
        Agent that owns the store this source belongs to.
    store : str
        Runtime store key the source belongs to, e.g. ``"claude.projects"``.
    adapter_id : str
        Versioned parser identity that would read this source, e.g.
        ``"claude.projects_jsonl.v1"``.
    path : str
        Display form of the discovered path, with the user's home abbreviated to ``~``.
    path_kind : t.Literal["history_file", "session_file", "sqlite_db", "store_file"]
        Filesystem entry the records live in.
    metadata : dict[str, t.Any]
        Discovery extras, such as the source's parse format. Empty when discovery
        recorded none.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    ref: str
    kind: t.Literal["find"]
    agent: t.Literal[
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
    store: str
    adapter_id: str
    path: str
    path_kind: t.Literal["history_file", "session_file", "sqlite_db", "store_file"]
    metadata: dict[str, t.Any] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: FindRecordLike) -> FindRecordModel:
        """Build a typed result from an ``agentgrep`` find record."""
        from agentgrep.mcp import refs

        payload = agentgrep.serialize_find_record(record)
        payload["ref"] = refs.make_find_ref(record)
        return cls.model_validate(payload)


class SourceVersionDetectionModel(AgentGrepModel):
    """Detected version metadata for one discovered source.

    Attributes
    ----------
    app_version : str | None
        Version of the agent that wrote the source. ``None`` when the detection pinned a
        data shape but learned no application version.
    data_version : str | None
        Version of the on-disk record shape the adapter parses. ``None`` when the shape
        was not pinned.
    strategy
        How the version was learned — a version probe, metadata embedded in the source,
        inference from the record shape, or the catalog's observed version.
    confidence : t.Literal["high", "medium", "low"]
        How much weight the detection carries.
    evidence : str
        Short note naming what was inspected, such as the object keys that decided a
        shape.
    """

    app_version: str | None = None
    data_version: str | None = None
    strategy: t.Literal[
        "version_check",
        "embedded_metadata",
        "shape_inference",
        "catalog_observation",
    ]
    confidence: t.Literal["high", "medium", "low"]
    evidence: str


class SourceRecordModel(AgentGrepModel):
    """Discovered source summary payload.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every emitted record so a reader can tell
        which wire shape it holds.
    agent
        Agent that owns the store this source belongs to.
    store : str
        Runtime store key, e.g. ``"claude.projects"``.
    adapter_id : str
        Versioned parser identity for this source, e.g. ``"claude.projects_jsonl.v1"``.
    path : str
        Display form of the source path, with the user's home abbreviated to ``~``.
    path_kind : t.Literal["history_file", "session_file", "sqlite_db", "store_file"]
        Filesystem entry the records live in.
    source_kind : t.Literal["json", "jsonl", "sqlite", "text", "opaque"]
        Parse format the adapter applies to the bytes.
    coverage : t.Literal["default_search", "inspectable", "catalog_only", "private"]
        Runtime search policy for the store, deciding which scopes open this source.
        ``inspectable`` sources are hidden from the default prompt scope but opened by a
        widened one; ``catalog_only`` and ``private`` sources are never searched.
    searchable : bool
        Whether search opens this source without an explicit opt-in. Mirrors
        ``search_by_default``, so an ``inspectable`` source reports ``False`` here even
        though a widened scope can still open it.
    search_by_default : bool
        Whether the source's coverage is ``default_search``, the always-on surface
        ordinary search and find flows read.
    searchable_reason : str
        Phrase explaining the search decision: searched by default, inspectable only, or
        catalog only.
    inspectable : bool
        Whether drilldown tools may read records from this source, true for
        ``default_search`` and ``inspectable`` coverage.
    version_detection : SourceVersionDetectionModel | None
        Detected app/data version for this concrete file or database. ``None`` when
        discovery skipped detection or learned nothing.
    search_root : str | None
        Display form of the directory the glob that found ``path`` was walked under,
        with a trailing separator. ``None`` for sources named by an exact filename.
    mtime_ns : int
        Modification time in nanoseconds, used for recency ordering and as a timestamp
        of last resort for stores that record none.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    agent: t.Literal[
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
    store: str
    adapter_id: str
    path: str
    path_kind: t.Literal["history_file", "session_file", "sqlite_db", "store_file"]
    source_kind: t.Literal["json", "jsonl", "sqlite", "text", "opaque"]
    coverage: t.Literal["default_search", "inspectable", "catalog_only", "private"]
    searchable: bool
    search_by_default: bool
    searchable_reason: str
    inspectable: bool
    version_detection: SourceVersionDetectionModel | None = None
    search_root: str | None = None
    mtime_ns: int

    @classmethod
    def from_source(cls, source: SourceHandleLike) -> SourceRecordModel:
        """Build a typed result from a discovered source."""
        payload = agentgrep.serialize_source_handle(source)
        coverage = str(payload["coverage"])
        search_by_default = coverage == "default_search"
        inspectable = coverage in {"default_search", "inspectable"}
        if search_by_default:
            searchable_reason = "searched by default"
        elif inspectable:
            searchable_reason = "inspectable only; not searched by default"
        else:
            searchable_reason = "catalog only; not searched by default"
        payload["searchable"] = search_by_default
        payload["search_by_default"] = search_by_default
        payload["searchable_reason"] = searchable_reason
        payload["inspectable"] = inspectable
        return cls.model_validate(payload)


class ResultStatsModel(AgentGrepModel):
    """Counters collected while building one MCP result page.

    Attributes
    ----------
    sources : int
        Sources the run planned to examine, reported when the run started.
    searched : int
        Tool-relative work counter: search reports records examined, while find reports
        sources examined.
    matched : int
        Records that matched across the whole run, not just the returned page.
    emitted : int
        Records placed on this page, at most the requested page size.
    """

    sources: int
    searched: int
    matched: int
    emitted: int


class PageInfoModel(AgentGrepModel):
    """Pagination metadata for a result page.

    Attributes
    ----------
    limit : int | None
        Page size the request asked for. ``None`` returns every record the run produced.
    count : int
        Records on this page.
    next_cursor : str | None
        Opaque cursor to pass back for the next page. ``None`` means no next page can be
        requested.
    """

    limit: int | None = None
    count: int
    next_cursor: str | None = None


class RunStatusModel(AgentGrepModel):
    """Search or find completion state.

    Attributes
    ----------
    state
        Terminal run state. ``complete`` means every planned source was examined;
        ``bounded`` means the run stopped at a requested bound such as a page limit, so
        more records may exist beyond it. ``truncated``, ``cancelled``, ``approximate``,
        and ``failed`` belong to the vocabulary but no code path emits them yet.
    reason : str | None
        Short machine-readable cause for a non-``complete`` state, such as
        ``"page_limit"``. ``None`` when the run completed.
    """

    state: t.Literal["complete", "bounded", "truncated", "cancelled", "approximate", "failed"]
    reason: str | None = None


class DiagnosticModel(AgentGrepModel):
    """Machine-readable result diagnostic.

    Attributes
    ----------
    code : str
        Stable identifier for the condition, such as ``"page_limit"``, meant for
        programmatic branching rather than display.
    message : str
        Readable explanation of the condition. Carries no prompt text, raw argv, or
        local absolute paths.
    """

    code: str
    message: str


class SearchRequestModel(AgentGrepModel):
    """Validated search request payload.

    Echoed back on the response so a stored payload stays self-describing. On a paged
    call the echoed values are the ones decoded from ``cursor``, not the arguments the
    caller repeated alongside it.

    Attributes
    ----------
    terms : list[str]
        Search terms, which accept agentgrep's query language: field predicates,
        booleans, phrases, and wildcards. Empty when an origin filter drives the search
        on its own.
    agent : AgentSelector
        Agent the search is limited to, or ``"all"`` for every agent.
    scope : SearchScopeName
        Stores the search opens: prompts, conversations, or all.
    case_sensitive : bool
        Whether matching is case-sensitive.
    limit : int | None
        Maximum records to return on one page. ``None`` returns every match.
    cursor : str | None
        Opaque page cursor from a previous search response. ``None`` starts a new
        search.
    cwd : str | None
        Only return records whose recorded working directory matches this path.
        ``None`` applies no cwd filter.
    repo : str | None
        Only return records whose recorded repository root matches this path. ``None``
        applies no repo filter.
    branch : str | None
        Only return records whose recorded git branch matches this name. ``None``
        applies no branch filter.
    """

    terms: list[str]
    agent: AgentSelector
    scope: SearchScopeName
    case_sensitive: bool
    limit: int | None = None
    cursor: str | None = None
    cwd: str | None = None
    repo: str | None = None
    branch: str | None = None


class SearchToolResponse(AgentGrepModel):
    """Structured response for the MCP search tool.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    request : SearchRequestModel
        Normalized request this page answers, with cursor-supplied values filled in.
    stats : ResultStatsModel
        Counters for the run behind this page.
    page : PageInfoModel
        Pagination metadata, including the cursor for the next page.
    status : RunStatusModel
        Terminal run state and its reason.
    diagnostics : list[DiagnosticModel]
        Warnings and errors raised while building the page. Empty when nothing needed
        reporting.
    results : list[SearchRecordModel]
        Matching records for this page, newest-first.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    request: SearchRequestModel
    stats: ResultStatsModel
    page: PageInfoModel
    status: RunStatusModel
    diagnostics: list[DiagnosticModel] = Field(default_factory=list)
    results: list[SearchRecordModel]


class FindRequestModel(AgentGrepModel):
    """Validated find request payload.

    Echoed back on the response so a stored payload stays self-describing. On a paged
    call the echoed values are the ones decoded from ``cursor``, not the arguments the
    caller repeated alongside it.

    Attributes
    ----------
    pattern : str | None
        Substring filter applied to discovered paths and adapter ids. ``None`` lists
        every discovered source.
    agent : AgentSelector
        Agent discovery is limited to, or ``"all"`` for every agent.
    limit : int | None
        Maximum sources to return on one page. ``None`` returns every discovered
        source.
    cursor : str | None
        Opaque page cursor from a previous find response. ``None`` starts a new
        listing.
    """

    pattern: str | None = None
    agent: AgentSelector
    limit: int | None = None
    cursor: str | None = None


class FindToolResponse(AgentGrepModel):
    """Structured response for the MCP find tool.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    request : FindRequestModel
        Normalized request this page answers, with cursor-supplied values filled in.
    stats : ResultStatsModel
        Counters for the run behind this page.
    page : PageInfoModel
        Pagination metadata, including the cursor for the next page.
    status : RunStatusModel
        Terminal run state and its reason.
    diagnostics : list[DiagnosticModel]
        Warnings and errors raised while building the page. Empty when nothing needed
        reporting.
    results : list[FindRecordModel]
        Discovered sources for this page, in discovery order.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    request: FindRequestModel
    stats: ResultStatsModel
    page: PageInfoModel
    status: RunStatusModel
    diagnostics: list[DiagnosticModel] = Field(default_factory=list)
    results: list[FindRecordModel]


class BackendAvailabilityModel(AgentGrepModel):
    """Selected read-only subprocess backends.

    Each field names the chosen executable without its machine-local path, so the
    summary says which backend is in use without leaking where it lives.

    Attributes
    ----------
    find_tool : str | None
        Executable selected for directory walks (``fd`` or ``fdfind``). ``None`` when
        neither is on ``PATH``, leaving the pure-Python walk in charge.
    grep_tool : str | None
        Executable selected for the root text prefilter (``rg`` or ``ag``). ``None``
        when neither is on ``PATH``, so every candidate source is opened and scanned.
    json_tool : str | None
        Executable selected for JSON and JSONL prefiltering (``jq`` or ``jaq``).
        ``None`` when neither is on ``PATH``.
    """

    find_tool: str | None = None
    grep_tool: str | None = None
    json_tool: str | None = None


class CapabilitiesModel(AgentGrepModel):
    """Static MCP capability summary.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    name : str
        Server name reported to clients.
    version : str
        Version of the MCP server surface, which the server advertises on connect and
        which moves independently of the agentgrep package version.
    read_only : bool
        Always ``True``: every tool and resource only reads local agent stores.
    agents
        Agents the server can search.
    search_scopes : list[SearchScopeName]
        Scope names the search tool accepts.
    adapters : list[str]
        Adapter ids the server knows, naming every parser it can apply to a discovered
        source.
    tools : list[str]
        Names of the registered MCP tools.
    resources : list[str]
        URIs of the registered MCP resources.
    prompts : list[str]
        Names of the registered MCP prompts.
    backends : BackendAvailabilityModel
        Optional subprocess backends resolved on this machine.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    name: str = "agentgrep"
    version: str = SERVER_VERSION
    read_only: bool = True
    agents: list[
        t.Literal[
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
    ]
    search_scopes: list[SearchScopeName]
    adapters: list[str]
    tools: list[str]
    resources: list[str]
    prompts: list[str]
    backends: BackendAvailabilityModel


SourceListAdapter = TypeAdapter(list[SourceRecordModel])
"""Serializer for the source-listing resources, whose payload is a JSON array."""


class StoreDescriptorModel(AgentGrepModel):
    """Catalog descriptor for one on-disk agent store.

    A descriptor is a snapshot of how the store looked when a contributor observed it,
    not a live reading of the filesystem. ``observed_version`` and ``observed_at`` stamp
    that snapshot so a reader can tell whether it is current.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    kind : t.Literal["store"]
        Constant tag marking a catalog descriptor rather than a record payload.
    agent
        CLI agent that owns this store.
    store_id : str
        Stable dotted identifier, e.g. ``"claude.projects.session"``.
    role : str
        Semantic role the store plays for the agent, e.g. ``"primary_chat"``, which
        informs the default search policy.
    format : str
        On-disk encoding, e.g. ``"jsonl"`` or ``"sqlite"``.
    path_pattern : str
        Path pattern with ``${HOME}``/``${<ENV>}`` and ``<placeholder>`` tokens, left
        unexpanded so the catalog stays portable.
    env_overrides : list[str]
        Environment variables that override the store root, e.g. ``["CODEX_HOME"]``.
        Empty when the location is fixed.
    platform_variants : dict[str, str]
        Per-platform path overrides keyed by ``"linux"``/``"darwin"``/``"win32"``. Empty
        when one pattern covers every platform.
    coverage : str
        Effective runtime coverage level, resolved from the catalog entry's explicit
        setting or inferred from its search policy and discovery specs.
    version_strategies : list[str]
        Strategies runtime discovery may use to identify a concrete source's version.
        Empty when none apply.
    observed_version : str
        Released version, or HEAD commit, the schema notes were captured against.
    observed_at : str | None
        ISO date the schema notes were captured. ``None`` when the entry records no
        date.
    upstream_ref : str | None
        Pointer to the authoritative upstream type definition. ``None`` when no upstream
        source is published.
    schema_notes : str
        Free-text description of the record shape.
    sample_record : str | None
        Short redacted sample of one record. ``None`` when the entry carries none.
    search_by_default : bool | None
        Whether agentgrep searches this store by default. ``None`` when the decision is
        deferred.
    search_notes : str | None
        Rationale for the search-policy decision, including de-duplication hints.
        ``None`` when none was recorded.
    distinguishes_from : list[str]
        Sibling ``store_id`` values this store overlaps with. Empty when nothing
        overlaps.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    kind: t.Literal["store"] = "store"
    agent: t.Literal[
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
    store_id: str
    role: str
    format: str
    path_pattern: str
    env_overrides: list[str] = Field(default_factory=list)
    platform_variants: dict[str, str] = Field(default_factory=dict)
    coverage: str
    version_strategies: list[str] = Field(default_factory=list)
    observed_version: str | None = None
    observed_at: str | None = None
    upstream_ref: str | None = None
    schema_notes: str | None = None
    sample_record: str | None = None
    search_by_default: bool | None = None
    search_notes: str | None = None
    distinguishes_from: list[str] = Field(default_factory=list)


class ListStoresRequest(AgentGrepModel):
    """Validated list-stores request payload.

    Attributes
    ----------
    agent : CatalogAgentSelector
        Catalog agent to filter to, including agents agentgrep documents but does not
        search, or ``"all"`` for every agent.
    role_filter : str | None
        One store-role value to filter on, e.g. ``"primary_chat"``. ``None`` returns
        every role.
    search_default_only : bool
        Return only stores that are searched by default.
    """

    agent: CatalogAgentSelector = "all"
    role_filter: str | None = None
    search_default_only: bool = False


class ListStoresResponse(AgentGrepModel):
    """Structured response for the MCP list_stores tool.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    stores : list[StoreDescriptorModel]
        Catalog descriptors that passed the request's filters.
    total : int
        Number of descriptors in ``stores``. This response is unpaged, so it counts the
        filtered selection rather than the whole catalog.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    stores: list[StoreDescriptorModel]
    total: int


class GetStoreDescriptorRequest(AgentGrepModel):
    """Validated get-store-descriptor request payload.

    Attributes
    ----------
    store_id : str
        Store id to look up, e.g. ``"claude.projects.session"``. An id no catalog entry
        carries is a tool error rather than an empty result.
    """

    store_id: str = Field(
        min_length=1,
        description="Store id (e.g. 'claude.projects.session').",
    )


class ListSourcesRequest(AgentGrepModel):
    """Validated list-sources request payload.

    Attributes
    ----------
    agent : AgentSelector
        Agent discovery is limited to, or ``"all"`` to scan every agent.
    path_kind_filter
        Keep only sources whose filesystem entry matches. ``None`` applies no path-kind
        filter.
    source_kind_filter
        Keep only sources whose parse format matches. ``None`` applies no source-kind
        filter.
    coverage_filter
        Keep only sources at this coverage level. ``None`` applies no coverage filter;
        setting it also admits non-default sources, so a filter for a non-default level
        has something to match.
    include_non_default : bool
        Include inventory-only sources that ordinary search skips.
    limit : int | None
        Stop after this many matching sources. ``None`` returns every match.
    """

    agent: AgentSelector = "all"
    path_kind_filter: (
        t.Literal["history_file", "session_file", "sqlite_db", "store_file"] | None
    ) = None
    source_kind_filter: t.Literal["json", "jsonl", "sqlite", "text", "opaque"] | None = None
    coverage_filter: (
        t.Literal["default_search", "inspectable", "catalog_only", "private"] | None
    ) = None
    include_non_default: bool = False
    limit: int | None = Field(default=None, ge=1)


class ListSourcesResponse(AgentGrepModel):
    """Structured response for the MCP list_sources tool.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    sources : list[SourceRecordModel]
        Discovered sources that passed the request's filters.
    total : int
        Number of sources in ``sources``. This response is unpaged, so it counts the
        returned selection rather than everything discovered.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    sources: list[SourceRecordModel]
    total: int


class FilterSourcesRequest(AgentGrepModel):
    """Validated filter-sources request payload.

    Attributes
    ----------
    pattern : str | None
        Substring a discovered path or adapter id must contain. Required unless
        ``cursor`` carries the pattern from an earlier page.
    agent : AgentSelector
        Agent discovery is limited to, or ``"all"`` to scan every agent.
    limit : int | None
        Maximum sources to return on one page. ``None`` returns every match.
    cursor : str | None
        Opaque page cursor from a previous filter_sources response. ``None`` starts a
        new listing.
    """

    pattern: str | None = Field(default=None, min_length=1)
    agent: AgentSelector = "all"
    limit: int | None = Field(default=50, ge=1)
    cursor: str | None = None


class DiscoverySummaryRequest(AgentGrepModel):
    """Validated summarize-discovery request payload.

    Attributes
    ----------
    agent : AgentSelector
        Agent discovery is limited to, or ``"all"`` to scan every agent.
    """

    agent: AgentSelector = "all"


class DiscoverySummaryResponse(AgentGrepModel):
    """Aggregate counts of discovered sources.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    total_sources : int
        Sources discovered for the selected agents.
    sources_by_agent : dict[str, int]
        Source counts keyed by agent name.
    sources_by_format : dict[str, int]
        Source counts keyed by parse format: ``json``, ``jsonl``, ``sqlite``, ``text``,
        or ``opaque``.
    sources_by_kind : dict[str, int]
        Source counts keyed by path kind: ``history_file``, ``session_file``,
        ``sqlite_db``, or ``store_file``.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    total_sources: int
    sources_by_agent: dict[str, int]
    sources_by_format: dict[str, int]
    sources_by_kind: dict[str, int]


VALIDATE_QUERY_INPUT_ERROR = "provide terms, query, or both"
"""Message returned when validate_query is called with neither terms nor a query."""


class ValidateQueryRequest(AgentGrepModel):
    """Validated validate-query request payload.

    Supply ``terms`` to dry-run literal/regex matching against
    ``sample_text``, ``query`` to validate query-language syntax, or both.

    Attributes
    ----------
    terms : list[str] | None
        Literal/regex terms to test against ``sample_text``. ``None`` or empty skips the
        dry-run, and is rejected unless ``query`` is supplied.
    query : str | None
        Query-language string to parse and compile. ``None`` skips syntax validation,
        and is rejected unless ``terms`` is supplied.
    case_sensitive : bool
        Whether the dry-run matches case-sensitively.
    sample_text : str
        Text the terms are tested against. Empty by default; no file is ever read.
    """

    terms: list[str] | None = None
    query: str | None = None
    case_sensitive: bool = False
    sample_text: str = ""

    @model_validator(mode="after")
    def _require_terms_or_query(self) -> ValidateQueryRequest:
        """Require at least one of ``terms`` or ``query``."""
        if not self.terms and self.query is None:
            raise ValueError(VALIDATE_QUERY_INPUT_ERROR)
        return self


class ValidateQueryResponse(AgentGrepModel):
    """Result of a dry-run query validation.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    matches : bool
        Whether the terms matched ``sample_text`` in the literal/regex dry-run.
        ``False`` when no terms were supplied.
    regex_valid : bool
        Whether the terms compiled as regular expressions. ``False`` only when a term
        raised a regex error.
    query_valid : bool | None
        Whether ``query`` parsed and compiled. ``None`` when no query was supplied.
    error_message : str | None
        Parse, compile, or regex error text. ``None`` when nothing failed.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    matches: bool
    regex_valid: bool
    query_valid: bool | None = None
    error_message: str | None = None


class RecentSessionsRequest(AgentGrepModel):
    """Validated recent-sessions request payload.

    Attributes
    ----------
    agent : AgentSelector
        Agent discovery is limited to, or ``"all"`` to scan every agent.
    hours : int
        How far back to look, in hours, capped at 30 days.
    limit : int | None
        Maximum sources to return. ``None`` returns every source inside the window.
    """

    agent: AgentSelector = "all"
    hours: int = Field(default=24, ge=1, le=24 * 30)
    limit: int | None = Field(default=10, ge=1)


class RecentSessionsResponse(AgentGrepModel):
    """Recently modified sources.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    cutoff_iso : str
        UTC ISO 8601 timestamp of the oldest modification time included, derived from
        the request's ``hours``.
    sources : list[SourceRecordModel]
        Sources modified at or after ``cutoff_iso``, newest-first.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    cutoff_iso: str
    sources: list[SourceRecordModel]


class InspectSampleRequest(AgentGrepModel):
    """Validated inspect-record-sample request payload.

    Attributes
    ----------
    adapter_id : str
        Adapter id of the source to read, e.g. ``"claude.projects_jsonl.v1"``. Paired
        with ``source_path`` to pick one discovered source.
    source_path : str
        Path of the source to read, as returned by list_sources. A ``~`` home prefix is
        accepted.
    sample_size : int
        Number of records to read, from 1 to 20.
    """

    adapter_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    sample_size: int = Field(default=1, ge=1, le=20)


class InspectResultRequest(AgentGrepModel):
    """Validated inspect-result request payload.

    Attributes
    ----------
    ref : str
        Opaque ref carried on a search or find result.
    sample_size : int
        Number of source records to read for a find ref, from 1 to 20. A search ref
        ignores it and resolves to the single record its fingerprint identifies.
    """

    ref: str = Field(min_length=1)
    sample_size: int = Field(default=1, ge=1, le=20)


class InspectSampleResponse(AgentGrepModel):
    """Sample records read from one source.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    adapter_id : str
        Adapter id the sample was requested for, echoed from the request.
    sample_count : int
        Records in ``records``. ``0`` when the source was not found or could not be
        read.
    records : list[SearchRecordModel]
        Records read from the source, in the order the adapter yields them.
    error_message : str | None
        Why the read produced nothing, such as a missing source or a parse failure.
        ``None`` when the read succeeded.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    adapter_id: str
    sample_count: int
    records: list[SearchRecordModel]
    error_message: str | None = None


class InspectResultResponse(AgentGrepModel):
    """Records read through an opaque result ref.

    Attributes
    ----------
    schema_version : str
        Payload schema version, stamped on every response so a reader can tell which
        wire shape it holds.
    ref : str
        Ref the records were resolved from, echoed from the request.
    sample_count : int
        Records in ``records``. ``0`` when the ref, its source, or the record behind it
        could not be resolved.
    records : list[SearchRecordModel]
        Records behind the ref. A search ref yields the one record whose fingerprint
        matches.
    error_message : str | None
        Why the ref resolved to nothing: an unparseable ref, a missing source, a record
        the source no longer holds, or a read failure. ``None`` when the read succeeded.
    """

    schema_version: str = agentgrep.SCHEMA_VERSION
    ref: str
    sample_count: int
    records: list[SearchRecordModel]
    error_message: str | None = None
