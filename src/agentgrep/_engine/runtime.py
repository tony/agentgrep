"""Reusable runtime state for headless search execution."""

from __future__ import annotations

import dataclasses

from agentgrep._engine.scanning import SourceScanCache


@dataclasses.dataclass(slots=True)
class SearchRuntime:
    """Reusable, explicit runtime state for one search frontend/session."""

    source_scan_cache: SourceScanCache | None = None

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
