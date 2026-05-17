"""Pydantic-backed catalogue of every on-disk store agentgrep knows about.

agentgrep searches AI agent prompt and history stores that live in the user's
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
    """On-disk encoding of a store's payload."""

    JSONL = "jsonl"
    JSON_ARRAY = "json_array"
    JSON_OBJECT = "json_object"
    SQLITE = "sqlite"
    MARKDOWN_FRONTMATTER = "md_frontmatter"
    PROTOBUF = "protobuf"
    OPAQUE = "opaque"


class StoreRole(enum.StrEnum):
    """Semantic role a store plays for the owning agent.

    The role drives the default search policy decisions downstream adapters
    make — chat transcripts are usually searched, app-state and cache stores
    are usually not. The role itself is descriptive; the policy decision is
    captured separately on each :class:`StoreDescriptor`.
    """

    PRIMARY_CHAT = "primary_chat"
    SUPPLEMENTARY_CHAT = "supplementary_chat"
    PROMPT_HISTORY = "prompt_history"
    PERSISTENT_MEMORY = "persistent_memory"
    PLAN = "plan"
    TODO = "todo"
    APP_STATE = "app_state"
    CACHE = "cache"
    SOURCE_TREE = "source_tree"
    UNKNOWN = "unknown"


AgentName = t.Literal["claude", "cursor", "codex", "gemini"]


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

    observed_version: str
    """Released version (or HEAD commit) the schema notes were captured against."""

    observed_at: datetime.date
    """Date the schema notes were captured."""

    upstream_ref: str | None = None
    """Pointer to the authoritative type definition.

    Example: ``github.com/openai/codex@4c89772/codex-rs/...#L2783``.
    """

    schema_notes: str
    """Free-text description of the record shape. Doctest-discouraged."""

    sample_record: str | None = None
    """A redacted, ~200-char sample of one record.

    Optional but recommended for primary-chat stores.
    """

    distinguishes_from: tuple[str, ...] = ()
    """Sibling ``store_id``s this store overlaps with; explains how they differ."""

    search_by_default: bool | None = None
    """Whether agentgrep should search this store by default.

    ``None`` means the decision is deferred.
    """

    search_notes: str | None = None
    """Free-text rationale for the search-policy decision, including de-duplication hints."""


class StoreCatalog(pydantic.BaseModel):
    """Versioned registry of every store agentgrep knows about."""

    model_config = pydantic.ConfigDict(frozen=True)

    catalog_version: int = 1
    """Bump on PRs that change descriptor shape or add/remove entries."""

    captured_at: datetime.date
    """Date the catalogue snapshot was taken."""

    stores: tuple[StoreDescriptor, ...]

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
    "AgentName",
    "StoreCatalog",
    "StoreDescriptor",
    "StoreFormat",
    "StoreRole",
)
