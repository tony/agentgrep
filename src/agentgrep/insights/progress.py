"""Progress protocol for long-running enrichment and model downloads.

Enrichers and the model downloader emit coarse phase progress through
this protocol so the CLI can render a spinner without the library
depending on a console. The protocol methods all have no-op defaults via
:class:`NoopInsightsProgress`, so callers may pass ``None``.
"""

from __future__ import annotations

import typing as t


@t.runtime_checkable
class InsightsProgress(t.Protocol):
    """A sink for phase-level progress during report enrichment."""

    def phase(self, name: str, *, detail: str = "") -> None:
        """Announce entry into a named phase (collect, analyze, summarize, …)."""
        ...

    def download_progress(
        self,
        *,
        model: str,
        downloaded_bytes: int,
        total_bytes: int | None,
    ) -> None:
        """Report bytes fetched for a model download."""
        ...

    def llm_chunk(
        self,
        *,
        backend: str,
        model: str,
        delta: str,
        char_count: int,
    ) -> None:
        """Report a streamed-token chunk from a local LLM.

        ``delta`` is the newly produced text so a console sink can render
        the summary live as it streams.
        """
        ...


class NoopInsightsProgress:
    """A progress sink that discards every event."""

    def phase(self, name: str, *, detail: str = "") -> None:
        """Discard the phase event."""

    def download_progress(
        self,
        *,
        model: str,
        downloaded_bytes: int,
        total_bytes: int | None,
    ) -> None:
        """Discard the download event."""

    def llm_chunk(
        self,
        *,
        backend: str,
        model: str,
        delta: str,
        char_count: int,
    ) -> None:
        """Discard the chunk event."""
