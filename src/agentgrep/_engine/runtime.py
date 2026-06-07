"""Reusable runtime state for headless search execution."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import typing as t

from agentgrep._engine.scanning import SourceScanCache

if t.TYPE_CHECKING:
    from agentgrep.db import DbRuntime

CacheMode = t.Literal["auto", "require", "off"]


@dataclasses.dataclass(slots=True)
class SearchRuntime:
    """Reusable, explicit runtime state for one search frontend/session."""

    source_scan_cache: SourceScanCache | None = None
    db: DbRuntime | None = None
    #: Per-consult DB factory for callers whose search work runs on a
    #: different thread than the one that built the runtime. SQLite
    #: connections are bound to their creating thread, so long-lived
    #: frontends (the MCP server) must open the cache in the consulting
    #: thread instead of holding ``db`` open. The opener returns ``None``
    #: when no usable cache exists; the consult closes what it opens.
    db_opener: cabc.Callable[[], DbRuntime | None] | None = None
    cache_mode: CacheMode = "auto"

    @classmethod
    def with_source_scan_cache(
        cls,
        *,
        max_entries: int = 512,
    ) -> SearchRuntime:
        """Return a runtime with a bounded source-scan cache."""
        return cls(
            source_scan_cache=SourceScanCache(max_entries=max_entries),
        )

    def clear_caches(self) -> None:
        """Clear every cache owned by this runtime."""
        if self.source_scan_cache is not None:
            self.source_scan_cache.clear()
