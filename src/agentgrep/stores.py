"""Pydantic-backed catalogue of every on-disk store agentgrep knows about.

agentgrep searches AI agent prompt and conversation stores that live in the user's
``$HOME``. Those stores move (Claude has renamed paths between minor
versions), grow (Cursor added a CLI agent with its own layout), and overlap
(Gemini keeps a pruned archive alongside its live tmp tree). Keeping that
knowledge as comments in adapter code makes it fragile: future readers can't
tell what the catalogue *was* at any given point, and there is no single
place to diff against when the next upstream rename lands.

This module defines the schema for the catalogue. ``store_catalog`` populates
it with the current entries; downstream adapters consume it.
"""

from __future__ import annotations

import datetime
import enum
import typing as t

import pydantic


class StoreFormat(enum.StrEnum):
    """On-disk encoding of a store's payload.

    Descriptive only: it tells a reader what the bytes look like, including for
    rows agentgrep never opens. The parse format an adapter actually uses is
    :attr:`DiscoverySpec.source_kind`.
    """

    JSONL = "jsonl"
    """One JSON object per line, appended as the agent writes records."""

    JSON_ARRAY = "json_array"
    """The whole file is one JSON array; each element is a record."""

    JSON_OBJECT = "json_object"
    """The whole file is one JSON object — a config, a task, a single document."""

    SQLITE = "sqlite"
    """A SQLite database; records live in tables an adapter queries."""

    TEXT = "text"
    """Plain text read as written, such as instruction files and logs."""

    MARKDOWN_FRONTMATTER = "md_frontmatter"
    """Markdown body under a YAML frontmatter header carrying the metadata."""

    PROTOBUF = "protobuf"
    """Protobuf-serialised payload, usually a ``.pb`` file.

    Some decode; the loose Antigravity and Windsurf conversation files are
    encrypted or custom-encoded and readable only as bytes.
    """

    OPAQUE = "opaque"
    """Bytes with no format agentgrep claims to read, or a whole directory tree.

    Secret stores, caches, and worktrees are catalogued for their location
    alone.
    """


class StoreRole(enum.StrEnum):
    """Semantic role a store plays for the owning agent.

    The role drives the default search policy decisions downstream adapters
    make — chat transcripts are usually searched, app-state and cache stores
    are usually not. The role itself is descriptive; the policy decision is
    captured separately on each :class:`StoreDescriptor`.
    """

    PRIMARY_CHAT = "primary_chat"
    """The agent's full per-thread transcript — its canonical conversation record.

    The role conversation scope enumerates. Prompt scope reaches it only for
    agents that have no ``PROMPT_HISTORY`` store of their own.
    """

    SUPPLEMENTARY_CHAT = "supplementary_chat"
    """Conversation content beside the primary transcript.

    Sub-agent transcripts, checkpoints, conversation summaries, and
    post-retention archives. Counted as a chat role for scope decisions, so it
    reaches the same scopes as ``PRIMARY_CHAT``.
    """

    PROMPT_HISTORY = "prompt_history"
    """A flat log of the prompts a user typed, appended across every thread.

    The store the default prompt scope goes to first. An agent that has one
    never falls back to its chat stores for that scope.
    """

    PERSISTENT_MEMORY = "persistent_memory"
    """Facts the agent carries between sessions.

    Memory files and memory tables steer future runs rather than recording a
    past one, so they stay out of default search.
    """

    PLAN = "plan"
    """A plan the agent wrote for a task, such as a plan file or a goal record."""

    TODO = "todo"
    """Task and todo lists the agent maintains for its own work in progress."""

    INSTRUCTION = "instruction"
    """Standing instructions loaded into the agent's behaviour.

    Skills, commands, rules, and project instruction files. They describe
    future sessions rather than record past ones.
    """

    APP_STATE = "app_state"
    """The agent's own bookkeeping: config, session indexes, logs, auth records.

    A few of these hold conversation content despite the role, and are admitted
    to conversation scope by name through
    :data:`~agentgrep.records.CONVERSATION_CONTENT_STORES` rather than by
    reclassifying the row.
    """

    CACHE = "cache"
    """Derived data the agent can regenerate: model caches, cloned repos, overflow output."""

    SOURCE_TREE = "source_tree"
    """Working copies of the user's code the agent kept — worktrees and file snapshots.

    Catalogued so an adapter does not index source code as history; searching
    one returns the user's files, not their conversations.
    """

    UNKNOWN = "unknown"
    """A store whose purpose has not been classified.

    Records a location before anyone has decided what it holds. No search
    policy keys off it.
    """


class StoreCoverage(enum.StrEnum):
    """How agentgrep treats a known store at runtime."""

    DEFAULT_SEARCH = "default_search"
    """Opened by normal search and find flows."""

    INSPECTABLE = "inspectable"
    """Hidden from the default prompt scope, but opt-in searchable.

    ``--scope conversations`` and ``--scope all`` open these, and inventory
    tools list them.
    """

    CATALOG_ONLY = "catalog_only"
    """Never searched at any scope.

    Inventory tools list them and ``find`` enumerates the ones that carry
    discovery specs, but their payloads are config, logs, caches, or
    undecodable bytes rather than recall content.
    """

    PRIVATE = "private"
    """Documented in the catalogue but intentionally not enumerated from disk."""


class VersionDetectionStrategy(enum.StrEnum):
    """How agentgrep detected a concrete source's app or data version."""

    VERSION_CHECK = "version_check"
    """A local version file supplied the app version, without spawning the agent's CLI."""

    EMBEDDED_METADATA = "embedded_metadata"
    """The source carried a version field of its own, such as a session metadata record."""

    SHAPE_INFERENCE = "shape_inference"
    """The file name, record keys, table names, or SQLite suffix identified the shape."""

    CATALOG_OBSERVATION = "catalog_observation"
    """No evidence came from the source, so the row's ``observed_version`` stands in.

    The fallback detection, always reported at
    :attr:`VersionDetectionConfidence.LOW`.
    """


class VersionDetectionConfidence(enum.StrEnum):
    """Confidence level for a detected source version."""

    HIGH = "high"
    """The source pinned itself: a version field it carries, or a key set unique to one shape."""

    MEDIUM = "medium"
    """The source parsed as its expected kind, but nothing in it named a version.

    The catalogue's shape is the best match rather than a proven one.
    """

    LOW = "low"
    """Nothing in the source spoke to its version; the catalogue stamp is all there is."""


AgentName = t.Literal[
    "claude",
    "cursor-cli",
    "cursor-ide",
    "codex",
    "gemini",
    "antigravity-cli",
    "antigravity-ide",
    "grok",
    "pi",
    "opencode",
    "windsurf",
    "vscode",
]
PathKind = t.Literal["history_file", "session_file", "sqlite_db", "store_file"]
SourceKind = t.Literal["json", "jsonl", "sqlite", "text", "opaque"]


class DiscoverySpec(pydantic.BaseModel):
    """Runtime metadata for discovering one store's source files.

    Catalogue rows whose store agentgrep actually scans at runtime carry a
    ``DiscoverySpec``. Rows that are documentary (planned support, opaque
    formats, source trees) leave ``StoreDescriptor.discovery`` as ``None``.

    Path resolution
    ---------------
    The discover function for an agent resolves a *base* directory
    (typically ``${HOME}/.<agent>`` or an env-override). The ``home_subpath``
    segments are appended to that base. Two enumeration modes are then
    available, used independently or together:

    - ``files`` lists specific relative filenames to check via ``is_file()``.
    - ``glob`` is a pattern walked under the resolved root via
      :func:`agentgrep.list_files_matching`. ``path_parts_required`` and
      ``path_parts_excluded`` filter glob results by path components (e.g.,
      Cursor CLI transcripts must live under ``agent-transcripts`` but the
      primary transcript store excludes nested ``subagents`` files).

    ``platform_paths`` lists absolute paths to check unconditionally, for
    stores whose canonical location depends on the operating system.
    """

    model_config = pydantic.ConfigDict(frozen=True)
    """Pydantic settings. Frozen: a spec is fixed once the catalogue is built."""

    store: str
    """Runtime store key (e.g. ``"claude.projects"``)."""

    adapter_id: str
    """Runtime adapter identifier (e.g. ``"claude.projects_jsonl.v1"``)."""

    data_version: str | None = None
    """Known data-shape version for this discovery path, when stable."""

    path_kind: PathKind
    """Kind of filesystem entry the records live in."""

    source_kind: SourceKind
    """Parse format (json / jsonl / sqlite)."""

    home_subpath: tuple[str, ...] = ()
    """Path segments appended to the agent's resolved base directory."""

    platform_paths: tuple[str, ...] = ()
    """Absolute paths to check unconditionally."""

    root_key: str = "default"
    """Named discovery root to resolve this spec against."""

    files: tuple[str, ...] = ()
    """Specific relative filenames to check via ``is_file()``."""

    glob: str | None = None
    """Glob pattern walked under the resolved root."""

    path_parts_required: tuple[str, ...] = ()
    """Each named segment must appear in a glob result's ``path.parts``."""

    path_parts_excluded: tuple[str, ...] = ()
    """A glob result is skipped when any named segment appears in ``path.parts``."""


class StoreDescriptor(pydantic.BaseModel):
    """One on-disk storage location for one CLI agent.

    Each descriptor is a snapshot of how the store looked when an agentgrep
    contributor observed it. The ``observed_version`` and ``observed_at``
    fields stamp that snapshot so future readers know whether a description
    is current or stale.

    Path patterns use ``${HOME}`` and ``${<ENV>}`` tokens so the catalogue
    stays portable. Resolving a pattern against a concrete environment is the
    consumer's job — adapters typically expand the tokens themselves.
    """

    model_config = pydantic.ConfigDict(frozen=True)
    """Pydantic settings. Frozen: a descriptor is fixed once the catalogue is built."""

    agent: AgentName
    """The CLI agent that owns this store."""

    store_id: str
    """Stable dotted identifier, e.g. ``claude.projects.session``."""

    role: StoreRole
    """Semantic role; informs default search policy."""

    format: StoreFormat
    """On-disk encoding."""

    path_pattern: str
    """Path pattern with ``${HOME}``/``${<ENV>}`` and ``<placeholder>`` tokens."""

    env_overrides: tuple[str, ...] = ()
    """Environment variables that override the root, e.g. ``("CODEX_HOME",)``."""

    platform_variants: dict[str, str] = pydantic.Field(default_factory=dict)
    """Per-platform path overrides keyed by ``"linux"``/``"darwin"``/``"win32"``."""

    coverage: StoreCoverage | None = None
    """Explicit runtime coverage level, or ``None`` to infer from search policy."""

    version_strategies: tuple[VersionDetectionStrategy, ...] = ()
    """Strategies runtime discovery may use to identify concrete source versions."""

    observed_version: str
    """Released version (or HEAD commit) the schema notes were captured against."""

    observed_at: datetime.date
    """Date the schema notes were captured."""

    upstream_ref: str | None = None
    """Pointer to the authoritative type definition.

    Example: ``github.com/openai/codex@3fb81667/codex-rs/...#L2929``.
    """

    schema_notes: str
    """Free-text description of the record shape. Doctest-discouraged."""

    sample_record: str | None = None
    """A redacted, ~200-char sample of one record.

    Optional but recommended for primary-chat stores.
    """

    distinguishes_from: tuple[str, ...] = ()
    """Sibling ``store_id`` values this store overlaps with; explains how they differ."""

    search_by_default: bool | None = None
    """Whether agentgrep should search this store by default.

    ``None`` means the decision is deferred.
    """

    search_notes: str | None = None
    """Free-text rationale for the search-policy decision, including de-duplication hints."""

    discovery: tuple[DiscoverySpec, ...] = ()
    """Runtime discovery specs for this store.

    Empty tuple means the row is documentary-only. Most discovered stores
    have exactly one entry; a few rows carry multiple specs because the
    same logical store has more than one on-disk shape — e.g. Codex's
    ``history.json`` and ``history.jsonl`` variants, or the modern vs.
    legacy Cursor IDE state databases.
    """

    @property
    def coverage_level(self) -> StoreCoverage:
        """Return this descriptor's effective runtime coverage level."""
        if self.coverage is not None:
            return self.coverage
        if self.search_by_default is True:
            return StoreCoverage.DEFAULT_SEARCH
        if self.discovery:
            return StoreCoverage.INSPECTABLE
        return StoreCoverage.CATALOG_ONLY


SEARCHABLE_COVERAGE: frozenset[StoreCoverage] = frozenset(
    {StoreCoverage.DEFAULT_SEARCH, StoreCoverage.INSPECTABLE},
)
"""Coverage levels a search may open.

``DEFAULT_SEARCH`` is the always-on surface; ``INSPECTABLE`` is the opt-in
surface a non-default scope unlocks. ``CATALOG_ONLY`` rows stay out even though
many of them carry discovery specs — the inventory-oriented
``include_non_default=True`` flag admits those specs so ``find`` can enumerate
them, so search has to re-narrow rather than inherit the flag's reach.
"""


class StoreCatalog(pydantic.BaseModel):
    """Versioned registry of every store agentgrep knows about."""

    model_config = pydantic.ConfigDict(frozen=True)
    """Pydantic settings. Frozen: the registry is fixed once it is built."""

    catalog_version: int = 1
    """Bump on PRs that change descriptor shape or add/remove entries."""

    captured_at: datetime.date
    """Date the catalogue snapshot was taken."""

    stores: tuple[StoreDescriptor, ...]
    """Every descriptor in the registry, grouped by owning agent.

    Lookups scan it in order; :meth:`by_id` and :meth:`for_agent` are the
    intended accessors.
    """

    def by_id(self, store_id: str) -> StoreDescriptor:
        """Return the descriptor with the given ``store_id``.

        Parameters
        ----------
        store_id : str
            The dotted identifier to look up.

        Returns
        -------
        StoreDescriptor
            The matching descriptor.

        Raises
        ------
        KeyError
            If no descriptor has that ``store_id``.
        """
        for store in self.stores:
            if store.store_id == store_id:
                return store
        raise KeyError(store_id)

    def for_agent(self, agent: AgentName) -> tuple[StoreDescriptor, ...]:
        """Return all descriptors owned by ``agent``."""
        return tuple(store for store in self.stores if store.agent == agent)


__all__ = (
    "SEARCHABLE_COVERAGE",
    "AgentName",
    "DiscoverySpec",
    "PathKind",
    "SourceKind",
    "StoreCatalog",
    "StoreCoverage",
    "StoreDescriptor",
    "StoreFormat",
    "StoreRole",
)
